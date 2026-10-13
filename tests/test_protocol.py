"""Protocol boundary and stream recovery tests; no serial device required."""
from pathlib import Path
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from protocol import (LineDecoder, MotionParameters, build_command,
                      encode_command, parse_targets)


STATUS = {"type": "omnipicker", "id": 1, "seq": 8, "event": "status",
          "fault": 0, "state": 1, "position": 128, "speed": 100, "force": 32}


def line(value, ending=b"\n"):
    return json.dumps(value, separators=(",", ":")).encode("ascii") + ending


class RequestTests(unittest.TestCase):
    def test_single_and_multi_id_fields(self):
        single = build_command("move", [1], MotionParameters(), 1)
        multi = build_command("move", [1, 2], MotionParameters(), 2)
        self.assertEqual(single["id"], 1)
        self.assertNotIn("ids", single)
        self.assertEqual(multi["ids"], [1, 2])
        self.assertNotIn("id", multi)

    def test_parameters_and_sequence_preserved_at_boundaries(self):
        command = build_command("move", [0, 2047], MotionParameters(
            position=0, speed=255, force=0, acceleration=255, deceleration=0),
            0xFFFFFFFF)
        self.assertEqual(command["position"], 0)
        self.assertEqual(command["force"], 0)
        self.assertEqual(command["seq"], 0xFFFFFFFF)
        self.assertEqual(json.loads(encode_command(command)), command)

    def test_open_close_omit_position_keep_force(self):
        for cmd in ("open", "close"):
            command = build_command(cmd, [1, 2], MotionParameters(force=60), 3)
            self.assertNotIn("position", command)
            self.assertEqual(command["force"], 60)
            self.assertEqual(command["speed"], 255)
            self.assertEqual(command["acceleration"], 255)
            self.assertEqual(command["deceleration"], 255)
            encode_command(command)

    def test_default_force_matches_working_reference_255(self):
        for cmd in ("move", "close", "open"):
            with self.subTest(cmd=cmd):
                self.assertEqual(build_command(cmd, [1], MotionParameters(), 1)["force"], 255)

    def test_id_decimal_hex_comma_whitespace(self):
        self.assertEqual(parse_targets(" 001, 0x02  3\t0X7ff "), [1, 2, 3, 2047])
        self.assertEqual(parse_targets("0"), [0])

    def test_bad_target_text_rejected(self):
        for text in ("", " ", "1,", ",1", "1,,2", "1, ,2", "-1", "+1",
                     "1.0", "0x", "1;2", "2048", "all", "*", "true"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_targets(text)

    def test_duplicate_target_not_silently_deduplicated(self):
        for text in ("1,1", "1 0x01", "0x7ff,2047"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_targets(text)

    def test_target_count_limit(self):
        self.assertEqual(len(parse_targets(",".join(map(str, range(16))))), 16)
        for targets in ([], list(range(17)), [1, 1], [True], [1.0], [-1], [2048]):
            with self.subTest(targets=targets), self.assertRaises(ValueError):
                build_command("move", targets, MotionParameters(), 1)

    def test_parameter_types_and_bounds(self):
        for name in ("position", "speed", "force", "acceleration", "deceleration"):
            for value in (-1, 256, True, 2.0, "32", None):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    build_command("move", [1], MotionParameters(**{name: value}), 1)

    def test_bad_sequence_or_command(self):
        for seq in (-1, 0x100000000, True, 1.0):
            with self.assertRaises(ValueError):
                build_command("move", [1], MotionParameters(), seq)
        for cmd in ("query", "stop", "Move", "all", None):
            with self.assertRaises(ValueError):
                build_command(cmd, [1], MotionParameters(), 1)

    def test_encoding_is_ascii_actual_single_lf_and_compact(self):
        command = build_command("move", list(range(2032, 2048)),
                                MotionParameters(255, 255, 255, 255, 255), 0xFFFFFFFF)
        payload = encode_command(command)
        self.assertLessEqual(len(payload), 384)
        self.assertTrue(payload.endswith(b"\n"))
        self.assertEqual(payload.count(b"\n"), 1)
        self.assertNotIn(b"\\n", payload)
        self.assertNotIn(b" ", payload)

    def test_manually_mutated_commands_validated(self):
        command = build_command("move", [1], MotionParameters(), 1)
        bad_commands = [dict(command, ids=[1, 2]), dict(command, all=True),
                        dict(command, type="can"), dict(command, position=True),
                        {key: value for key, value in command.items() if key != "force"},
                        dict(command, cmd="close"), dict(command, id="1")]
        for value in bad_commands:
            with self.subTest(value=value), self.assertRaises(ValueError):
                encode_command(value)

    def test_minimal_open_is_valid(self):
        command = {"type": "omnipicker", "id": 1, "cmd": "open"}
        self.assertEqual(json.loads(encode_command(command)), command)


class DecoderTests(unittest.TestCase):
    def test_fragmented_line_and_coalesced_crlf(self):
        decoder = LineDecoder()
        payload = line(STATUS, b"\r\n")
        self.assertEqual(decoder.feed(payload[:17]), [])
        self.assertEqual(decoder.feed(payload[17:-1]), [])
        self.assertEqual(decoder.feed(payload[-1:] + line(dict(STATUS, id=2))),
                         [STATUS, dict(STATUS, id=2)])
        self.assertEqual(decoder.invalid_lines, 0)

    def test_bad_json_recovers_next_line(self):
        decoder = LineDecoder()
        self.assertEqual(decoder.feed(b'{"position":0"speed":255}\n' + line(STATUS)), [STATUS])
        self.assertEqual(decoder.invalid_lines, 1)

    def test_error_with_and_without_id_sequence(self):
        replies = [{"type": "omnipicker", "event": "error", "error": "invalid_json"},
                   {"type": "omnipicker", "id": 2, "seq": 5,
                    "event": "error", "error": "timeout"},
                   {"type": "omnipicker", "event": "error",
                    "error": "response_overflow", "rejected": 4294967295}]
        decoder = LineDecoder()
        self.assertEqual(decoder.feed(b"".join(map(line, replies))), replies)

    def test_invalid_feedback_types_unknown_fields_are_rejected(self):
        replies = [dict(STATUS, type="can"), dict(STATUS, event="tx_queued"),
                   dict(STATUS, id=True), dict(STATUS, id=2048),
                   dict(STATUS, force=256), dict(STATUS, speed=-1),
                   dict(STATUS, state=1.0), dict(STATUS, seq=4294967296),
                   dict(STATUS, unknown=0),
                   {key: value for key, value in STATUS.items() if key != "fault"},
                   {"type": "omnipicker", "event": "error", "error": ""},
                   {"type": "omnipicker", "event": "error", "error": "timeout",
                    "rejected": True}]
        decoder = LineDecoder()
        self.assertEqual(decoder.feed(b"".join(map(line, replies)) + line(STATUS)), [STATUS])
        self.assertEqual(decoder.invalid_lines, len(replies))

    def test_duplicate_fields_and_nonfinite_values_rejected(self):
        decoder = LineDecoder()
        bad = (b'{"type":"omnipicker","type":"omnipicker","event":"error",'
               b'"error":"invalid_json"}\n'
               b'{"type":"omnipicker","event":"error","error":"timeout","seq":NaN}\n')
        self.assertEqual(decoder.feed(bad + line(STATUS)), [STATUS])
        self.assertEqual(decoder.invalid_lines, 2)

    def test_nul_non_ascii_bom_and_noise_recover(self):
        decoder = LineDecoder()
        bad = b"\x00\n\xff\n\xef\xbb\xbf{}\nboot log\n[]\n\n"
        self.assertEqual(decoder.feed(bad + line(STATUS)), [STATUS])
        self.assertEqual(decoder.invalid_lines, 6)

    def test_overlong_line_counted_once_and_buffer_bounded(self):
        decoder = LineDecoder()
        self.assertEqual(decoder.feed(b"x" * 100_000), [])
        self.assertLessEqual(len(decoder._buffer), 385)
        self.assertEqual(decoder.invalid_lines, 1)
        self.assertEqual(decoder.overlong_lines, 1)
        self.assertEqual(decoder.feed(b"junk\n" + line(STATUS)), [STATUS])
        self.assertEqual(decoder.discarded_bytes, 100_004)

    def test_line_limit_excludes_crlf(self):
        payload = line(STATUS)[:-1]
        padded = payload + b" " * (384 - len(payload))
        decoder = LineDecoder()
        self.assertEqual(decoder.feed(padded + b"\r\n"), [STATUS])
        self.assertEqual(decoder.invalid_lines, 0)
        self.assertEqual(decoder.feed(padded + b" \n" + line(STATUS)), [STATUS])
        self.assertEqual(decoder.overlong_lines, 1)

    def test_new_decoder_does_not_carry_old_connection_half_line(self):
        old = LineDecoder()
        self.assertEqual(old.feed(line(STATUS)[:10]), [])
        new = LineDecoder()
        self.assertEqual(new.feed(line(STATUS)), [STATUS])


if __name__ == "__main__":
    unittest.main()
