"""
Automated unit tests for ADB Alert & Bulk Call Manager Pro.
"""

import unittest

from adb_alert_call_manager_pro import (
    CONFIG_FILE,
    TerminalCaptureStream,
    _mmi_safe_number,
    is_incoming_call_ringing,
    normalize_number,
    parse_call_state_text,
    parse_numbers_from_text,
)


class TestCallManager(unittest.TestCase):
    def test_normalize_number(self):
        # Indian 10-digit standard
        self.assertEqual(normalize_number("9820806052"), "+919820806052")
        # Leading zero 11-digit
        self.assertEqual(normalize_number("09820806052"), "+919820806052")
        # International with +
        self.assertEqual(normalize_number("+919820806052"), "+919820806052")
        # 00 international prefix
        self.assertEqual(normalize_number("00919820806052"), "+919820806052")
        # MMI codes passed through unchanged
        self.assertEqual(normalize_number("*21*9820806052#"), "*21*9820806052#")
        self.assertEqual(normalize_number("##21#"), "##21#")
        # Invalid inputs
        self.assertIsNone(normalize_number(""))
        self.assertIsNone(normalize_number("abc"))
        self.assertIsNone(normalize_number("123"))

    def test_mmi_safe_number(self):
        self.assertEqual(_mmi_safe_number("+919820806052"), "919820806052")
        self.assertEqual(_mmi_safe_number("9820806052"), "9820806052")

    def test_parse_numbers_from_text(self):
        text = """
        9820806052, +919820806053; 09820806054
        9820806052
        invalid_item
        """
        result = parse_numbers_from_text(text)
        expected = ["+919820806052", "+919820806053", "+919820806054"]
        self.assertEqual(result, expected)

    def test_parse_call_state_idle(self):
        dump = """
        mCallState=0
        mCallState=0
        mRingingCallState=0
        mForegroundCallState=0
        """
        self.assertEqual(parse_call_state_text(dump), "IDLE")

    def test_parse_call_state_active(self):
        dump = """
        mCallState=2
        mRingingCallState=0
        mForegroundCallState=1
        """
        self.assertEqual(parse_call_state_text(dump), "ACTIVE")

    def test_parse_call_state_dialing_alerting(self):
        dialing_dump = "mForegroundCallState=3"
        self.assertEqual(parse_call_state_text(dialing_dump), "DIALING")

        alerting_dump = "mForegroundCallState=4"
        self.assertEqual(parse_call_state_text(alerting_dump), "ALERTING")

    def test_parse_call_state_ringing(self):
        ringing_dump = "mRingingCallState=1"
        self.assertEqual(parse_call_state_text(ringing_dump), "RINGING")
        self.assertTrue(is_incoming_call_ringing(ringing_dump))

    def test_parse_call_state_multisim(self):
        # SIM 1 is idle, SIM 2 is active
        multisim_dump = """
        Phone Id=0
          mCallState=0
          mForegroundCallState=0
        Phone Id=1
          mCallState=2
          mForegroundCallState=1
        """
        self.assertEqual(parse_call_state_text(multisim_dump), "ACTIVE")

    def test_parse_call_state_disconnecting(self):
        # PreciseCallState 8 = DISCONNECTING
        disconnecting_dump = "mForegroundCallState=8"
        self.assertEqual(parse_call_state_text(disconnecting_dump), "DISCONNECTING")

    def test_terminal_capture_stream(self):
        stream = TerminalCaptureStream(None, CONFIG_FILE)
        self.assertFalse(stream.isatty())
        stream.flush()

if __name__ == "__main__":
    unittest.main()
