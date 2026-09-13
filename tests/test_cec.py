import time
import unittest
from unittest.mock import patch

from pipertv import cec
from pipertv.cec import (CecCtlParser, CecMonitor, CecState, format_address,
                         physical_address)

PI = "1.0.0.0"

# One State Change event, then the TV announcing the Pi's branch as active.
MONITOR_OUTPUT = ("Initial Event: State Change: PA: 1.0.0.0, LA mask: 0x0010\\n"
                  "Received from TV to all (0 to 15): ACTIVE_SOURCE (0x82):\\n"
                  "\\tRaw: 0x0f 0x82 0x10 0x00\\n")


def frame(*values):
    return bytes(values)


def fake_monitor(output=MONITOR_OUTPUT):
    """Stand in for cec-ctl without needing an HDMI adapter or privileges."""
    return ["sh", "-c", f"printf '{output}'; sleep 3"]


class AddressTests(unittest.TestCase):
    def test_dotted_and_packed_forms_agree(self):
        self.assertEqual(physical_address("1.0.0.0"), 0x1000)
        self.assertEqual(physical_address("1000"), 0x1000)
        self.assertEqual(physical_address("0x1000"), 0x1000)
        self.assertEqual(physical_address(0x2100), 0x2100)
        self.assertEqual(format_address(0x2100), "2.1.0.0")

    def test_unknown_address_is_none(self):
        self.assertIsNone(physical_address(0xFFFF))
        self.assertIsNone(physical_address("f.f.f.f"))
        self.assertIsNone(physical_address(None))
        self.assertIsNone(format_address(None))

    def test_addresses_that_do_not_describe_a_topology_are_rejected(self):
        # CEC fills address digits from the left; a gap cannot describe a path.
        for value in ("0.1.0.0", "1.0.1.0", "1.0.1", "0x10000", -1, 0x10000, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                physical_address(value)


class StateTests(unittest.TestCase):
    def test_starts_unknown_until_the_tv_reports_a_source(self):
        state = CecState(PI)
        self.assertEqual(state.snapshot()["state"], "unknown")
        self.assertEqual(state.snapshot()["physical_address"], PI)

    def test_without_an_address_no_frame_is_evidence(self):
        state = CecState(None)
        self.assertFalse(state.observe(frame(0x0F, 0x82, 0x10, 0x00)))
        self.assertEqual(state.snapshot()["state"], "unknown")

    def test_routing_messages_select_and_deselect_the_pi(self):
        state = CecState(PI)
        cases = [
            ("active source is the Pi", frame(0x4F, 0x82, 0x10, 0x00), "active"),
            ("active source is elsewhere", frame(0x2F, 0x82, 0x20, 0x00), "inactive"),
            ("TV routes to the Pi", frame(0x0F, 0x80, 0x20, 0x00, 0x10, 0x00), "active"),
            ("TV routes away", frame(0x0F, 0x80, 0x10, 0x00, 0x30, 0x00), "inactive"),
            ("TV sets the stream path here", frame(0x0F, 0x86, 0x10, 0x00), "active"),
            ("TV reports routing here", frame(0x0F, 0x81, 0x10, 0x00), "active"),
        ]
        for name, message, expected in cases:
            with self.subTest(name=name):
                self.assertTrue(state.observe(message))
                self.assertEqual(state.snapshot()["state"], expected)

    def test_only_the_tv_may_report_routing(self):
        state = CecState(PI)
        state.observe(frame(0x4F, 0x82, 0x10, 0x00))
        # A source on another branch cannot establish what the TV displays.
        for message in (frame(0x4F, 0x80, 0x20, 0x00, 0x30, 0x00),
                        frame(0x4F, 0x86, 0x30, 0x00),
                        frame(0x4F, 0x81, 0x30, 0x00)):
            with self.subTest(message=message.hex()):
                self.assertFalse(state.observe(message))
        self.assertEqual(state.snapshot()["state"], "active")

    def test_inactive_source_and_standby_turn_control_off(self):
        for name, message in [("the Pi went inactive", frame(0x40, 0x9D, 0x10, 0x00)),
                              ("standby was requested", frame(0x0F, 0x36)),
                              ("the TV reports standby", frame(0x04, 0x90, 0x01))]:
            with self.subTest(name=name):
                state = CecState(PI)
                state.observe(frame(0x4F, 0x82, 0x10, 0x00))
                self.assertTrue(state.observe(message))
                self.assertEqual(state.snapshot()["state"], "inactive")

    def test_powering_on_after_standby_is_unknown_not_active(self):
        state = CecState(PI)
        state.observe(frame(0x0F, 0x36))
        self.assertEqual(state.snapshot()["state"], "inactive")
        state.observe(frame(0x04, 0x90, 0x00))
        # The TV is on, but nothing says which input it chose.
        self.assertEqual(state.snapshot()["state"], "unknown")

    def test_transmitted_frames_are_never_evidence(self):
        state = CecState(PI)
        self.assertFalse(state.observe(frame(0x4F, 0x82, 0x10, 0x00), received=False))
        self.assertEqual(state.snapshot()["state"], "unknown")

    def test_malformed_frames_are_ignored(self):
        state = CecState(PI)
        for message in (frame(0x0F), frame(0x0F, 0x82, 0x10), frame(0xFF, 0x82, 0x10, 0x00),
                        frame(0x40, 0x82, 0x10, 0x00), frame(0x0F, 0x80, 0x10, 0x00),
                        frame(0x0F, 0x82, 0xFF, 0xFF), b"", "not bytes"):
            with self.subTest(message=message):
                self.assertFalse(state.observe(message))
        self.assertEqual(state.snapshot()["state"], "unknown")

    def test_positive_evidence_expires_but_negative_evidence_does_not(self):
        now = [0.0]
        state = CecState(PI, stale_after_s=5, clock=lambda: now[0])
        state.observe(frame(0x4F, 0x82, 0x10, 0x00))
        self.assertEqual(state.snapshot()["state"], "active")
        now[0] = 6
        self.assertEqual(state.snapshot()["state"], "unknown")

        now[0] = 10
        state.observe(frame(0x2F, 0x82, 0x20, 0x00))
        now[0] = 100
        # Nothing said the Pi came back, so "not selected" must not decay to active.
        self.assertEqual(state.snapshot()["state"], "inactive")

    def test_evidence_lifetime_is_bounded(self):
        for value in (0.5, 3601, True, float("nan"), "120"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CecState(PI, stale_after_s=value)

    def test_changing_the_hdmi_address_clears_earlier_evidence(self):
        state = CecState(PI)
        state.observe(frame(0x4F, 0x82, 0x10, 0x00))
        state.set_physical_address("2.0.0.0")
        self.assertEqual(state.snapshot()["state"], "unknown")
        self.assertEqual(state.snapshot()["physical_address"], "2.0.0.0")

    def test_repeated_reports_refresh_evidence_without_starting_another_visit(self):
        state = CecState(PI)
        state.observe(frame(0x0F, 0x86, 0x10, 0x00))
        first = state.snapshot()
        state.observe(frame(0x0F, 0x81, 0x10, 0x00))
        again = state.snapshot()
        self.assertEqual(again["selection_revision"], first["selection_revision"])
        self.assertGreater(again["evidence_revision"], first["evidence_revision"])
        state.observe(frame(0x0F, 0x80, 0x10, 0x00, 0x20, 0x00))
        state.observe(frame(0x0F, 0x86, 0x10, 0x00))
        self.assertGreater(state.snapshot()["selection_revision"], again["selection_revision"])

    def test_unrelated_traffic_does_not_refresh_selection_evidence(self):
        now = [0.0]
        state = CecState(PI, stale_after_s=5, clock=lambda: now[0])
        state.observe(frame(0x0F, 0x86, 0x10, 0x00))
        first = state.snapshot()
        now[0] = 4
        # A powered-on TV and an OSD name say nothing about the selected input.
        state.observe(frame(0x04, 0x90, 0x00))
        state.observe(frame(0x04, 0x47, 0x54, 0x56))
        self.assertEqual(state.snapshot()["evidence_revision"], first["evidence_revision"])
        now[0] = 5
        expired = state.snapshot()
        self.assertEqual(expired["state"], "unknown")
        self.assertGreater(expired["selection_revision"], first["selection_revision"])
        self.assertEqual(state.snapshot()["selection_revision"], expired["selection_revision"])

    def test_address_revision_remembers_a_disconnection_between_polls(self):
        state = CecState(PI)
        first = state.snapshot()
        state.set_physical_address(None)
        state.set_physical_address(PI)
        returned = state.snapshot()
        self.assertEqual(returned["physical_address"], first["physical_address"])
        self.assertGreater(returned["address_revision"], first["address_revision"])


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.state = CecState()
        self.parser = CecCtlParser(self.state)

    def feed(self, *lines):
        return [self.parser.feed_line(line) for line in lines]

    def test_state_change_supplies_the_address_and_a_raw_frame_is_evidence(self):
        evidence = self.feed("Initial Event: State Change: PA: 1.0.0.0, LA mask: 0x0010",
                             "Received from TV to all (0 to 15): ACTIVE_SOURCE (0x82):",
                             "\tphys-addr: 1.0.0.0",
                             "\tRaw: 0x0f 0x82 0x10 0x00")
        self.assertEqual(evidence, [False, False, False, True])
        self.assertTrue(self.parser.ready)
        self.assertEqual(self.state.snapshot()["state"], "active")

    def test_raw_frame_must_match_its_announced_header(self):
        self.feed("Initial Event: State Change: PA: 1.0.0.0, LA mask: 0x0010",
                  "Received from TV to all (0 to 15): ACTIVE_SOURCE (0x82):")
        self.assertFalse(self.parser.feed_line("\tRaw: 0x4f 0x82 0x10 0x00"))
        self.assertEqual(self.state.snapshot()["state"], "unknown")

    def test_malformed_raw_tokens_are_ignored_and_the_parser_recovers(self):
        self.feed("Initial Event: State Change: PA: 1.0.0.0, LA mask: 0x0010")
        header = "Received from TV to all (0 to 15): ACTIVE_SOURCE (0x82):"
        for raw in ("Raw: 0x0f0x82 0x10 0x00", "Raw: 0x0f 0x82 0x100 0x00",
                    "Raw: 0x0f 0x82 0x10 0x00 trailing garbage"):
            with self.subTest(raw=raw):
                self.feed(header)
                self.assertFalse(self.parser.feed_line(raw))
                self.assertEqual(self.state.snapshot()["state"], "unknown")
        self.feed(header)
        self.assertTrue(self.parser.feed_line("\tRaw: 0x0f 0x82 0x10 0x00 (    )"))
        self.assertEqual(self.state.snapshot()["state"], "active")

    def test_transmitted_frames_from_the_log_are_not_evidence(self):
        self.feed("Initial Event: State Change: PA: 1.0.0.0, LA mask: 0x0010",
                  "Transmitted by TV to all (0 to 15): ACTIVE_SOURCE (0x82):")
        self.assertFalse(self.parser.feed_line("\tRaw: 0x0f 0x82 0x10 0x00"))
        self.assertEqual(self.state.snapshot()["state"], "unknown")

    def test_lost_messages_discard_the_current_conclusion(self):
        self.feed("Initial Event: State Change: PA: 1.0.0.0, LA mask: 0x0010",
                  "Received from TV to all (0 to 15): ACTIVE_SOURCE (0x82):",
                  "\tRaw: 0x0f 0x82 0x10 0x00")
        self.assertEqual(self.state.snapshot()["state"], "active")
        self.parser.feed_line("Event: Lost 5 messages")
        self.assertEqual(self.state.snapshot()["state"], "unknown")

    def test_monitor_errors_are_reported(self):
        for line in ("sudo: a password is required",
                     "Failed to open /dev/cec0: Permission denied",
                     "error: cannot open device",
                     "Event: Disconnected"):
            with self.subTest(line=line):
                state = CecState(PI)
                parser = CecCtlParser(state)
                parser.feed_line(line)
                self.assertTrue(parser.failed)
                self.assertEqual(state.snapshot()["state"], "unknown")

    def test_text_supplied_by_the_tv_cannot_stop_monitoring(self):
        # cec-ctl indents decoded payloads. A TV names itself and writes OSD
        # strings, so that text must never be read as this monitor failing.
        state = CecState(PI)
        parser = CecCtlParser(state)
        state.observe(frame(0x4F, 0x82, 0x10, 0x00))
        for payload in ("\tosd: MyTV Error: tuner failed",
                        "\tosd name: Failed Devices Inc",
                        "\trec-status: error: no media",
                        "\tvendor-id: 0x000000 (Permission denied)"):
            with self.subTest(payload=payload.strip()):
                parser.feed_line(payload)
                self.assertFalse(parser.failed)
                self.assertEqual(state.snapshot()["state"], "active")

    def test_payload_inside_a_message_block_is_not_an_error(self):
        state = CecState(PI)
        parser = CecCtlParser(state)
        state.observe(frame(0x4F, 0x82, 0x10, 0x00))
        parser.feed_line("Received from TV to all (0 to 15): SET_OSD_STRING (0x64):")
        parser.feed_line("osd: playback failed")
        self.assertFalse(parser.failed)
        self.assertEqual(state.snapshot()["state"], "active")

    def test_oversized_output_is_refused(self):
        self.assertFalse(self.parser.feed_line("x" * 8193))
        self.assertTrue(self.parser.failed)


class MonitorTests(unittest.TestCase):
    def test_device_and_command_are_validated(self):
        for kwargs in ({"device": "/dev/video0"}, {"device": "/dev/cec0; rm -rf /"},
                       {"device": 0}, {"command": []}, {"command": ["cec-ctl", 5]},
                       {"command": "cec-ctl"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                CecMonitor(**kwargs)

    def test_default_command_only_monitors_and_never_transmits(self):
        command = CecMonitor(privileged=True)._command()
        self.assertEqual(command[:2], ["sudo", "-n"])
        self.assertIn("--monitor", command)
        self.assertIn("--show-raw", command)
        self.assertNotIn("--playback", command)
        self.assertNotIn("--active-source", command)
        self.assertNotIn("sudo", CecMonitor(privileged=False)._command())

    def test_monitoring_starts_without_privileges(self):
        # Being in the video group is enough to open /dev/cec0, though not to
        # select monitor mode; PiperTV must never wait on a sudo password.
        self.assertNotIn("sudo", CecMonitor()._command())

    def test_a_refused_monitor_mode_is_recognised_and_explained(self):
        # Verified on Raspberry Pi OS: cec-ctl prints this and then exits 0.
        state = CecState(PI)
        parser = CecCtlParser(state)
        parser.feed_line("Selecting monitor mode failed, you may have to run this as root.")
        self.assertTrue(parser.failed)
        self.assertTrue(parser.needs_root)
        reason = state.snapshot()["reason"]
        self.assertIn("CAP_NET_ADMIN", reason)
        self.assertIn("manual confirmation", reason)

    def test_other_failures_are_not_blamed_on_privileges(self):
        state = CecState(PI)
        parser = CecCtlParser(state)
        parser.feed_line("Failed to open /dev/cec0: No such file or directory")
        self.assertTrue(parser.failed)
        self.assertFalse(parser.needs_root)
        self.assertNotIn("CAP_NET_ADMIN", state.snapshot()["reason"])

    def test_selection_is_unknown_while_the_monitor_is_not_running(self):
        monitor = CecMonitor()
        monitor.state.set_physical_address(PI)
        monitor.state.observe(frame(0x0F, 0x82, 0x10, 0x00))
        self.assertEqual(monitor.state.snapshot()["state"], "active")
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot["state"], "unknown")
        self.assertFalse(snapshot["monitor_running"])

    def test_unreadable_adapter_still_reports_selection_through_cec_ctl(self):
        # /dev/cec0 is normally root:video, so the unprivileged Flask process
        # cannot read the address itself; the privileged monitor reports it.
        monitor = CecMonitor(privileged=False, command=fake_monitor())
        self.addCleanup(monitor.close)
        with patch.object(cec, "read_physical_address",
                          side_effect=PermissionError(13, "Permission denied")):
            monitor.start()
            snapshot = self.wait_for(monitor, "active")
        self.assertEqual(snapshot["state"], "active")
        self.assertEqual(snapshot["physical_address"], PI)
        self.assertTrue(snapshot["monitor_running"])

    def test_readable_adapter_reports_selection(self):
        monitor = CecMonitor(privileged=False, command=fake_monitor())
        self.addCleanup(monitor.close)
        with patch.object(cec, "read_physical_address", return_value=0x1000):
            monitor.start()
            snapshot = self.wait_for(monitor, "active")
        self.assertEqual(snapshot["state"], "active")
        self.assertTrue(snapshot["monitor_running"])

    def test_closing_stops_reporting_a_selection(self):
        monitor = CecMonitor(privileged=False, command=fake_monitor())
        with patch.object(cec, "read_physical_address", return_value=0x1000):
            monitor.start()
            self.wait_for(monitor, "active")
        monitor.close()
        self.assertEqual(monitor.snapshot()["state"], "unknown")

    def wait_for(self, monitor, state, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = monitor.snapshot()
            if snapshot["state"] == state:
                return snapshot
            time.sleep(0.02)
        self.fail(f"CEC state stayed {monitor.snapshot()['state']!r}, expected {state!r}")


if __name__ == "__main__":
    unittest.main()
