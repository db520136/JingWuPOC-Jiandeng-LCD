"""Host regression tests; hardware interfaces are replaced with small fakes."""
import _thread
import binascii
import importlib
import pathlib
import sys
import threading
import time
import types
import unittest
from unittest.mock import Mock

ROOT = pathlib.Path(__file__).resolve().parents[1] / "code"
_thread.allocate_semphore = threading.Semaphore
usr = types.ModuleType("usr")
usr.__path__ = [str(ROOT / "poc_comm"), str(ROOT / "poc_ui")]
sys.modules["usr"] = usr


class Clock:
    now = 0
    MASK = (1 << 30) - 1

    @classmethod
    def add(cls, value, delta):
        return (value + delta) & cls.MASK

    @classmethod
    def diff(cls, first, second):
        half = (cls.MASK + 1) // 2
        return (first - second + half) % (cls.MASK + 1) - half


utime = types.ModuleType("utime")
utime.ticks_ms = lambda: Clock.now
utime.ticks_add = Clock.add
utime.ticks_diff = Clock.diff
utime.sleep_ms = lambda value: None
utime.localtime = lambda: (2026, 10, 27, 12, 34, 56, 1, 300)
sys.modules["utime"] = utime
sys.modules["ubinascii"] = binascii


class Timer:
    Timer0, Timer1, Timer2, Timer3 = range(4)
    ONE_SHOT, PERIODIC = range(2)

    def __init__(self, number):
        self.number = number
        self.period = None
        self.start_count = 0
        self.stop_count = 0

    def start(self, period, mode, callback):
        self.start_count += 1
        self.mode = mode
        self.period, self.callback = period, callback

    def stop(self):
        self.stop_count += 1
        self.period = None

    def fire(self):
        if self.mode == self.ONE_SHOT:
            self.period = None
        self.callback(self)


machine = types.ModuleType("machine")
machine.Timer = Timer
sys.modules["machine"] = machine


def stub(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    sys.modules[name] = module
    return module


stub("usr.audio_port", AudioPort=object, play_forced_logout_tts=lambda value: None)
stub("usr.communication_runtime", CommunicationRuntime=object)
stub("usr.communication_session", CommunicationSession=object)
stub("usr.http_directory_worker", HTTPDirectoryWorker=object,
     HTTP_REQUEST_TIMEOUT_S=5, fetch_directory=lambda *args: None)
stub("lvgl", color_hex=lambda value: value,
     TEXT_ALIGN=types.SimpleNamespace(LEFT=0, CENTER=1),
     PART=types.SimpleNamespace(MAIN=0),
     obj=types.SimpleNamespace(FLAG=types.SimpleNamespace(HIDDEN=1)))
stub("usr.font_port", **{name: object() for name in (
    "text_style_blue text_style_green text_style_red text_style_white "
    "status_style_white regular_style_white regular_style_green "
    "regular_style_blue regular_style_red").split()})
stub("usr.display_port", DisplayPort=object)
stub("usr.touch_port", TouchPort=object)
stub("usr.ota_service", OTA_PROGRESS_DOT_INTERVAL_MS=500, OTA_PROGRESS_DOT_MAX=3)

state_module = importlib.import_module("usr.ui_state")
messages_module = importlib.import_module("usr.ui_messages")
queue_module = importlib.import_module("usr.ui_commands")
keys = importlib.import_module("usr.hardware_key_port")
device_module = importlib.import_module("usr.device_port")
protocol = importlib.import_module("usr.poc_protocol")
workers = importlib.import_module("usr.low_priority_worker")
controller_module = importlib.import_module("usr.communication_controller")
pages_module = importlib.import_module("usr.ui_pages")
app_module = importlib.import_module("usr.ui_app")


def location(fix=True, altitude=10):
    return dict(state=2, fix=fix, latitude=30.0 if fix else None,
                longitude=120.0 if fix else None, altitude=altitude if fix else None,
                satellites=8, raw_gga=b"$GNGGA,test*00" if fix else b"")


class Device:
    gnss_initialized = True

    def __init__(self, result=None):
        self.result = result if result is not None else location()
        self.reads = 0

    def initialize_gnss(self):
        return self.gnss_initialized

    def read_location(self):
        self.reads += 1
        return dict(self.result)


def gnss(device=None):
    state = state_module.UIStateStore()
    results = []
    worker = workers.GnssWorker(device or Device(), state,
                               lambda value: results.append(value) or True,
                               lambda *args: None)
    worker._init_due = None
    return worker, state, results


def controller():
    state = state_module.UIStateStore()
    state.update_business_page_active(True)
    instance = controller_module.CommunicationController(
        {"device_id": "YNPOC110160930100001"}, state, messages_module.UIMessageBus())
    instance.running = True
    instance.police_no = "M100001"
    instance.login_state = "logged_in"
    instance._auto_reconnect_enabled = True
    instance._network_ready = instance._tcp_ready = True
    instance.tcp_socket = object()
    instance.runtime = types.SimpleNamespace(running=True)
    instance._manual_gnss_request = lambda ticket: True
    return instance, state


class GNSSCheckinTests(unittest.TestCase):
    def setUp(self):
        Clock.now = 0

    def test_latest_valid_gga_survives_invalid_tail_and_bad_checksum(self):
        def gga(quality, altitude):
            body = f"GNGGA,123456,3000.0,N,12000.0,E,{quality},08,1.0,{altitude},M,0,M,,"
            checksum = 0
            for value in body.encode():
                checksum ^= value
            return f"${body}*{checksum:02X}\r\n"
        raw = (gga(1, 10) + gga(1, 20) + gga(0, 0) + "$GNGGA,bad*00\r\n").encode()
        device_module.quecgnss = types.SimpleNamespace(
            get_state=lambda: 2, read=lambda size: (len(raw), raw))
        port = device_module.DevicePort()
        port.gnss_initialized = True
        result = port.read_location()
        self.assertTrue(result["fix"])
        self.assertEqual(result["altitude"], 20)
        device_module.quecgnss.read = lambda size: -1
        self.assertFalse(port.read_location()["fix"])

    def test_protocol_type_length_and_crc(self):
        for kind in (0, 1):
            payload = protocol.build_gnss_upload_payload("YNPOC110160930100001", b"abc", kind)
            self.assertEqual(payload[20:], bytes((kind, 3)) + b"abc")
            frame = protocol.build_frame(protocol.MSG_GNSS_UPLOAD, 65535, payload)
            self.assertEqual(int.from_bytes(frame[-2:], "big"),
                             protocol.crc16_ccitt_false(frame[:-2]))

    def test_four_attempts_wait_ten_seconds_after_each_return(self):
        worker, state, results = gnss(Device(location(False)))
        ticket = dict(id=1, cancelled=False)
        worker._manual = ticket
        worker._manual_due = 0
        for attempt in range(4):
            worker._service()
            self.assertEqual(worker.device.reads, attempt + 1)
            if attempt < 3:
                self.assertFalse(results)
                self.assertEqual(worker._manual_due, Clock.now + 10000)
                Clock.now += 9999
                worker._service()
                self.assertEqual(worker.device.reads, attempt + 1)
                Clock.now += 1
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["fix"])
        self.assertIsNone(worker._manual)

    def test_initialization_failure_also_consumes_four_attempts(self):
        device = Device()
        device.gnss_initialized = False
        worker, state, results = gnss(device)
        worker._manual = dict(id=1, cancelled=False)
        worker._manual_due = 0
        for _ in range(4):
            worker._service()
            Clock.now += 10000
        self.assertEqual(device.reads, 0)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["fix"])

    def test_read_errors_retry_four_times_instead_of_failing_immediately(self):
        device = Device()
        device.read_location = Mock(side_effect=OSError("read failed"))
        worker, state, results = gnss(device)
        worker._manual = dict(id=1, cancelled=False)
        worker._manual_due = 0
        for attempt in range(4):
            worker._service()
            self.assertEqual(len(results), 1 if attempt == 3 else 0)
            Clock.now += 10000
        self.assertEqual(device.read_location.call_count, 4)

    def test_manual_success_does_not_change_auto_baseline_or_deadline(self):
        worker, state, results = gnss()
        baseline = location(altitude=20)
        worker._last_valid_location = baseline
        worker._auto_due = 30000
        worker._manual = dict(id=1, cancelled=False)
        worker._manual_due = 0
        worker._service()
        self.assertEqual(len(results), 1)
        self.assertIs(worker._last_valid_location, baseline)
        self.assertEqual(worker._auto_due, 30000)
        self.assertIn("checkin_ticket", results[0])

    def test_auto_fast_phase_has_ten_attempts_and_normal_thirty_seconds(self):
        worker, state, results = gnss(Device(location(False)))
        worker._auto_enabled = True
        worker._start_fast()
        for attempt in range(10):
            worker._service()
            interval = 5000 if attempt < 9 else 30000
            self.assertEqual(worker._auto_due, Clock.now + interval)
            Clock.now += interval
        self.assertFalse(worker._auto_fast)

    def test_fast_phase_stops_on_first_valid_fix(self):
        worker, state, results = gnss()
        worker._auto_enabled = True
        worker._start_fast()
        worker._service()
        self.assertFalse(worker._auto_fast)
        self.assertEqual(worker._auto_due, 30000)

    def test_cancel_during_read_ignores_late_result(self):
        worker, state, results = gnss()
        ticket = dict(id=1, cancelled=False)
        worker._manual = ticket
        worker._manual_due = 0
        def read():
            ticket["cancelled"] = True
            return location()
        worker.device.read_location = read
        worker._service()
        self.assertFalse(results)

    def test_touch_defers_capture_and_result_processing(self):
        worker, state, results = gnss()
        worker._manual = dict(id=1, cancelled=False)
        worker._manual_due = 0
        worker.set_ui_busy(True)
        worker._service()
        self.assertEqual(worker.device.reads, 0)
        worker.set_ui_busy(False)
        def read():
            worker.set_ui_busy(True)
            return location()
        worker.device.read_location = read
        worker._service()
        self.assertFalse(results)
        worker.set_ui_busy(False)
        worker._service()
        self.assertEqual(len(results), 1)

    def test_real_thread_blocks_until_manual_event_and_touch_release(self):
        worker, state, results = gnss()
        done = threading.Event()
        worker.result_callback = lambda result: results.append(result) or done.set() or True
        worker.set_ui_busy(True)
        worker.start()
        try:
            worker.request_manual(dict(id=1, cancelled=False))
            self.assertFalse(done.wait(0.03))
            self.assertEqual(worker.device.reads, 0)
            worker.set_ui_busy(False)
            self.assertTrue(done.wait(1))
            self.assertEqual(worker.device.reads, 1)
            self.assertEqual(len(results), 1)
        finally:
            worker.stop()
            deadline = time.monotonic() + 1
            while worker.thread_id is not None and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertIsNone(worker.thread_id)

    def test_tick_wrap_deadline(self):
        worker, state, results = gnss(Device(location(False)))
        Clock.now = Clock.MASK - 5000
        worker._manual = dict(id=1, cancelled=False)
        worker._manual_due = Clock.now
        worker._service()
        Clock.now = Clock.add(Clock.now, 10000)
        worker._service()
        self.assertEqual(worker.device.reads, 2)

    def test_manual_and_auto_have_separate_controller_result_slots(self):
        instance, state = controller()
        self.assertTrue(instance.request_checkin())
        ticket = instance._checkin
        instance.request_gnss_result(dict(location(), checkin_ticket=ticket))
        instance.request_gnss_result(dict(location(), session_generation=0))
        instance.request_gnss_result(dict(location(altitude=20), session_generation=0))
        self.assertIs(ticket["result"]["checkin_ticket"], ticket)
        self.assertEqual(len(instance._requests), 1)
        self.assertEqual(instance._requests[0][1]["altitude"], 20)
        self.assertFalse(instance.request_checkin())

    def test_poc_off_disconnected_floor_still_allows_manual_upload(self):
        instance, state = controller()
        instance._poc_enabled = False
        state._update({"floor_state": "disconnected"})
        instance.request_checkin()
        instance.request_gnss_result(dict(location(), checkin_ticket=instance._checkin))
        sent = []
        instance._send_tcp_transaction = lambda *args, **kwargs: sent.append(args) or {"payload": b""}
        instance._service_checkin()
        self.assertEqual(state.get_snapshot()[1]["checkin_phase"], "success")
        self.assertEqual(sent[0][1][9 + 20], 1)
        self.assertIsNone(instance._gnss_last_uploaded_fix)

    def test_rx_delays_send_and_auto_cannot_overwrite_it(self):
        instance, state = controller()
        instance.request_checkin()
        instance.request_gnss_result(dict(location(), checkin_ticket=instance._checkin))
        state._update({"audio_rx_active": True})
        instance._upload_gnss = Mock(return_value=True)
        instance._service_checkin()
        instance._upload_gnss.assert_not_called()
        self.assertIsNotNone(instance._checkin)
        state._update({"audio_rx_active": False})
        instance._service_checkin()
        instance._upload_gnss.assert_called_once()
        self.assertEqual(state.get_snapshot()[1]["checkin_phase"], "success")

    def test_no_fix_and_send_failure_have_different_outcomes(self):
        instance, state = controller()
        instance.request_checkin()
        instance.request_gnss_result(dict(location(False), checkin_ticket=instance._checkin))
        instance._service_checkin()
        self.assertEqual(state.get_snapshot()[1]["checkin_phase"], "location_failed")
        instance.request_checkin()
        instance.request_gnss_result(dict(location(), checkin_ticket=instance._checkin))
        instance._upload_gnss = Mock(side_effect=OSError(104))
        instance._service_checkin()
        self.assertEqual(state.get_snapshot()[1]["checkin_phase"], "send_failed")

    def test_cancel_and_old_session_results_cannot_complete_new_checkin(self):
        instance, state = controller()
        instance.request_checkin()
        old = instance._checkin
        instance._cancel_checkin()
        instance.request_checkin()
        current = instance._checkin
        instance.request_gnss_result(dict(location(), checkin_ticket=old))
        self.assertIsNone(current["result"])
        self.assertFalse(instance._finish_checkin(old, "success"))
        self.assertEqual(state.get_snapshot()[1]["checkin_id"], current["id"])

    def test_guest_login_and_logout_cannot_start_checkin(self):
        for flag in ("_guest_mode", "_logout_requested", "_server_logout_pending", "_ota_busy"):
            instance, state = controller()
            setattr(instance, flag, True)
            self.assertFalse(instance.request_checkin(), flag)
        instance, state = controller()
        instance.police_no = None
        self.assertFalse(instance.request_checkin())

    def test_clock_and_gnss_updates_do_not_overwrite_each_other(self):
        worker, state, results = gnss()
        state.update_location_clock("12:00:01", "星期二")
        worker._display(location())
        self.assertEqual(state.get_snapshot()[1]["location_time"], "12:00:01")
        state.update_location_clock("12:00:02", "星期二")
        self.assertEqual(state.get_snapshot()[1]["latitude"], 30.0)

    def test_volume_up_and_down_are_short_press_only(self):
        state = state_module.UIStateStore()
        actions = []
        worker = workers.LowPriorityWorker(state_store=state,
            key_action_callback=actions.append)
        worker._handle_key_action(keys.KEY_VOLUME_UP)
        worker._handle_key_action(keys.KEY_VOLUME_DOWN)
        self.assertEqual(actions, [keys.KEY_VOLUME_UP, keys.KEY_VOLUME_DOWN])
        state.update_screen_sleeping(True)
        worker._handle_key_action(keys.KEY_VOLUME_UP)
        worker._handle_key_action(keys.KEY_VOLUME_DOWN)
        self.assertEqual(actions, [keys.KEY_VOLUME_UP, keys.KEY_VOLUME_DOWN])

    def test_lamp_short_press_toggles_on_release_and_long_press_checks_in(self):
        class KeyPort:
            def is_key_pressed(self, _key_name):
                return True
        requests = []
        worker = workers.LowPriorityWorker(
            checkin_request_callback=lambda: requests.append(True) or True)
        worker.hardware_keys = KeyPort()
        worker._toggle_lamp = Mock()
        worker._wake_screen = Mock()
        worker._handle_key_action(keys.KEY_LAMP_PRESS, 100)
        self.assertEqual(worker._toggle_lamp.call_count, 0)
        worker._handle_key_action(keys.KEY_LAMP_RELEASE, 500)
        worker._toggle_lamp.assert_called_once_with("GPIO28短按松开")

        worker._handle_key_action(keys.KEY_LAMP_PRESS, 1000)
        Clock.now = 2500
        self.assertTrue(worker._check_lamp_key_long())
        self.assertEqual(requests, [True])
        worker._handle_key_action(keys.KEY_LAMP_RELEASE, 2600)
        self.assertEqual(worker._toggle_lamp.call_count, 1)
        worker._wake_screen.assert_called_once_with("手动打卡")

    def test_lamp_edges_do_not_produce_dump_debug_logs(self):
        class EdgePort:
            batch_through_ms = 1000
            def take_key_events(self):
                return (("lamp", True, 100), ("lamp", False, 600))
            def take_event_overflow(self):
                return 0
            def take_irq_diagnostics(self):
                return 2, 100, 600
        worker = workers.LowPriorityWorker(hardware_keys=EdgePort())
        worker._key_stable_pressed["lamp"] = False
        worker._debug_key_release = Mock()
        worker._toggle_lamp = Mock()
        Clock.now = 1000
        worker._handle_hardware_key_irq()
        worker._toggle_lamp.assert_called_once_with("GPIO28短按松开")

    def test_double_click_defaults_off_but_saved_setting_is_preserved(self):
        self.assertFalse(state_module.UIStateStore().get_snapshot()[1][
            "double_click_enabled"])
        self.assertTrue(state_module.UIStateStore(initial_settings={
            "double_click_enabled": True}).get_snapshot()[1]["double_click_enabled"])

    def test_lamp_long_timer_and_release_do_not_duplicate_checkin(self):
        class KeyPort:
            def is_key_pressed(self, _key_name):
                return True
        requests = []
        worker = workers.LowPriorityWorker(
            checkin_request_callback=lambda: requests.append(True) or False)
        worker.hardware_keys = KeyPort()
        worker._handle_lamp_key(keys.KEY_LAMP_PRESS, 0)
        Clock.now = 1500
        self.assertTrue(worker._check_lamp_key_long())
        worker._handle_lamp_key(keys.KEY_LAMP_RELEASE, 1600)
        self.assertEqual(requests, [True])
        self.assertFalse(worker._lamp_long_fired)

    def test_rejected_lamp_long_press_is_consumed_without_waking_or_toggling(self):
        class KeyPort:
            def is_key_pressed(self, _key_name):
                return True
        requests = []
        state = state_module.UIStateStore()
        state.update_screen_sleeping(True)
        worker = workers.LowPriorityWorker(state_store=state,
            checkin_request_callback=lambda: requests.append(True) or False)
        worker.hardware_keys = KeyPort()
        worker._wake_screen = Mock()
        worker._toggle_lamp = Mock()
        worker._handle_lamp_key(keys.KEY_LAMP_PRESS, 0)
        Clock.now = workers.LAMP_KEY_LONG_PRESS_MS
        self.assertTrue(worker._check_lamp_key_long())
        worker._handle_lamp_key(keys.KEY_LAMP_RELEASE,
            workers.LAMP_KEY_LONG_PRESS_MS + 100)
        self.assertEqual(requests, [True])
        worker._wake_screen.assert_not_called()
        worker._toggle_lamp.assert_not_called()

    def test_lamp_short_press_while_sleeping_toggles_without_waking(self):
        state = state_module.UIStateStore()
        state.update_screen_sleeping(True)
        worker = workers.LowPriorityWorker(state_store=state)
        worker._wake_screen = Mock()
        worker._toggle_lamp = Mock()
        worker._handle_key_action(keys.KEY_LAMP_PRESS, 10)
        worker._handle_key_action(keys.KEY_LAMP_RELEASE, 110)
        worker._toggle_lamp.assert_called_once_with("GPIO28短按松开")
        worker._wake_screen.assert_not_called()

    def test_short_release_edge_wins_over_delayed_long_press_check(self):
        requests = []
        worker = workers.LowPriorityWorker(
            checkin_request_callback=lambda: requests.append(True) or False)
        worker._handle_lamp_key(keys.KEY_LAMP_PRESS, 0)
        Clock.now = 1500
        worker._handle_lamp_key(keys.KEY_LAMP_RELEASE, 1500)
        self.assertEqual(requests, [True])
        self.assertFalse(worker._lamp_long_fired)

    def test_deferred_events_do_not_wake_until_touch_releases(self):
        queue = queue_module.UICommandQueue()
        queue.set_ui_busy(True)
        queue.put("manual", 1, defer_if_busy=True)
        queue.put_latest("auto", 2, defer_if_busy=True)
        queue.signal_periodic_isr()
        self.assertFalse(queue._wake.acquire(blocking=False))
        queue.set_ui_busy(False)
        self.assertTrue(queue._wake.acquire(blocking=False))
        events = queue.take_events()
        self.assertTrue(events[2])
        self.assertEqual(len(events[6]), 2)
        self.assertFalse(queue._wake.acquire(blocking=False))

    def test_key_poll_consumes_wake_token_before_taking_events(self):
        queue = queue_module.UICommandQueue()
        self.assertFalse(queue.try_wait())
        queue.signal_hardware_key_isr()
        self.assertTrue(queue.try_wait())
        self.assertTrue(queue.take_events()[8])
        queue.signal_hardware_key_isr()
        self.assertTrue(queue.try_wait())
        self.assertTrue(queue.take_events()[8])
        self.assertFalse(queue.try_wait())

    def test_duplicate_isr_wake_does_not_raise_when_token_is_already_available(self):
        queue = queue_module.UICommandQueue()
        queue.wake()
        queue._wake_pending = False
        self.assertTrue(queue.signal_hardware_key_isr())
        self.assertTrue(queue._wake_pending)
        self.assertTrue(queue.try_wait())

    def test_event_arriving_during_take_events_is_not_lost(self):
        queue = queue_module.UICommandQueue()
        class InterruptDuringSort(list):
            def sort(self, **kwargs):
                queue.signal_periodic_isr()
                super().sort(**kwargs)
        queue._items = InterruptDuringSort()
        queue.signal_periodic_isr()
        queue.wait()
        queue.take_events()
        self.assertTrue(queue._wake.acquire(blocking=False))
        self.assertTrue(queue.take_events()[2])

    def test_manual_retransmission_uses_same_packet_and_matching_ack(self):
        instance, state = controller()
        instance.request_checkin()
        instance.request_gnss_result(dict(location(), checkin_ticket=instance._checkin))
        packets = []
        def submit(command, packet):
            packets.append(packet)
            if len(packets) == 2:
                instance._events.append(dict(source="tcp", event=dict(type="tcp_frame",
                    payload=dict(msg_type=protocol.MSG_GNSS_UPLOAD_ACK,
                                 request_cnt=2, payload=b""))))
            return True
        instance.runtime.submit = submit
        previous = utime.sleep_ms
        utime.sleep_ms = lambda delta: setattr(Clock, "now", Clock.add(Clock.now, delta))
        try:
            instance._service_checkin()
        finally:
            utime.sleep_ms = previous
        self.assertEqual(len(packets), 2)
        self.assertEqual(packets[0], packets[1])
        self.assertEqual(state.get_snapshot()[1]["checkin_phase"], "success")

    def test_exhausted_tcp_retries_finish_checkin_with_send_failure(self):
        instance, state = controller()
        instance.request_checkin()
        instance.request_gnss_result(dict(location(), checkin_ticket=instance._checkin))
        instance.runtime.submit = Mock(return_value=True)
        instance._schedule_reconnect = Mock()
        previous = utime.sleep_ms
        utime.sleep_ms = lambda delta: setattr(Clock, "now", Clock.add(Clock.now, delta))
        try:
            instance._service_checkin()
        finally:
            utime.sleep_ms = previous
        self.assertEqual(instance.runtime.submit.call_count, 4)
        self.assertEqual(Clock.now, 5000)
        instance._schedule_reconnect.assert_called_once()
        self.assertEqual(state.get_snapshot()[1]["checkin_phase"], "send_failed")

    def test_cancel_while_waiting_for_ack_prevents_further_retries(self):
        instance, state = controller()
        instance.request_checkin()
        instance.request_gnss_result(dict(location(), checkin_ticket=instance._checkin))
        instance.runtime.submit = Mock(return_value=True)
        previous = utime.sleep_ms
        def cancel(delta):
            instance._cancel_checkin()
            Clock.now += delta
        utime.sleep_ms = cancel
        try:
            instance._service_checkin()
        finally:
            utime.sleep_ms = previous
        self.assertEqual(instance.runtime.submit.call_count, 1)
        self.assertEqual(state.get_snapshot()[1]["checkin_phase"], "cancelled")
        self.assertIsNone(instance._active_tcp_transaction)

    def test_auto_waiting_for_business_idle_can_resume_without_session(self):
        worker, state, results = gnss()
        worker._auto_enabled = True
        worker._auto_due = 0
        state._update({"ota_busy": True})
        worker._service()
        worker._plan()
        self.assertTrue(worker._auto_waiting)
        self.assertIsNone(worker._next_due)
        state._update({"ota_busy": False})
        worker.resume_auto_if_idle()
        self.assertEqual(worker.events.pending_count(), 1)

    def make_popup(self):
        page = pages_module.PageFactory.__new__(pages_module.PageFactory)
        for attribute in ("_confirm_overlay", "_confirm_dialog", "_confirm_label",
                          "_popup_line2", "_set_popup_buttons", "_set_popup_locked"):
            setattr(page, attribute, Mock())
        page._pending_confirm = None
        page._popup_mode = page._popup_signature = page._popup_until_ms = None
        return page

    def test_popup_colors_duration_and_new_progress_replaces_old_timer(self):
        page = self.make_popup()
        self.assertTrue(page.show_checkin_popup(1, "locating"))
        self.assertIsNone(page._popup_until_ms)
        page._confirm_dialog.set_style_bg_color.assert_called_with(pages_module.GREEN, 0)
        self.assertTrue(page.show_checkin_popup(1, "location_failed"))
        self.assertEqual(page._popup_until_ms, 2000)
        page._confirm_dialog.set_style_bg_color.assert_called_with(pages_module.RED, 0)
        Clock.now = 1000
        self.assertTrue(page.show_checkin_popup(2, "locating"))
        Clock.now = 2000
        page.process_popup()
        self.assertEqual(page._popup_signature, ("checkin", 2, "locating"))
        page.show_checkin_popup(2, "success")
        page._confirm_dialog.set_style_bg_color.assert_called_with(pages_module.GREEN, 0)
        Clock.now = 4000
        page.process_popup()
        self.assertIsNone(page._popup_mode)

    def test_high_priority_popup_wins_and_progress_resumes_afterwards(self):
        page = self.make_popup()
        app = app_module.NavigationTestApp.__new__(app_module.NavigationTestApp)
        app.pages = page
        app._logged_in = app._checkin_popup_allowed = True
        app._checkin_view = (1, "sending")
        app._checkin_popup_seen = None
        page._popup_mode, page._popup_signature = "call", ("call", 1)
        app._process_checkin_popup()
        self.assertEqual(page._popup_signature, ("call", 1))
        page._hide_popup()
        app._process_checkin_popup()
        self.assertEqual(page._popup_signature, ("checkin", 1, "sending"))
        app._checkin_view = (1, "cancelled")
        app._process_checkin_popup()
        self.assertIsNone(page._popup_mode)

    def test_result_popup_only_appears_once(self):
        page = self.make_popup()
        app = app_module.NavigationTestApp.__new__(app_module.NavigationTestApp)
        app.pages = page
        app._logged_in = app._checkin_popup_allowed = True
        app._checkin_view = (1, "success")
        app._checkin_popup_seen = None
        app._process_checkin_popup()
        Clock.now = 2000
        page.process_popup()
        app._process_checkin_popup()
        self.assertIsNone(page._popup_mode)

    def test_checkin_prevents_sleep_but_releases_sleep_gate_when_finished(self):
        state = state_module.UIStateStore()
        state.update_business_page_active(True)
        state.update_sleep_timeout(10)
        device = types.SimpleNamespace(screen_sleeping=False, screen_off=Mock())
        worker = workers.LowPriorityWorker(state_store=state, device=device)
        state.update_checkin(1, "locating")
        Clock.now = 60000
        worker._check_sleep_timeout()
        device.screen_off.assert_not_called()
        state.update_checkin(1, "success")
        worker._service_sleep_deadline()
        Clock.now += 14000
        worker._check_sleep_timeout()
        device.screen_off.assert_called_once()

    def test_tcp_disconnect_finishes_checkin_before_reconnect_cleanup(self):
        instance, state = controller()
        instance.request_checkin()
        ticket = instance._checkin
        instance.runtime = None
        instance.tcp_socket = Mock()
        instance._schedule_reconnect("test disconnect")
        self.assertTrue(ticket["cancelled"])
        self.assertIsNone(instance._checkin)
        self.assertEqual(state.get_snapshot()[1]["checkin_phase"], "send_failed")
        instance.request_gnss_result(dict(location(), checkin_ticket=ticket))
        self.assertEqual(state.get_snapshot()[1]["checkin_phase"], "send_failed")

    def test_logout_isolation_cancels_before_closing_connection(self):
        instance, state = controller()
        instance.request_checkin()
        ticket = instance._checkin
        instance.runtime = None
        instance._begin_logout_isolation()
        self.assertTrue(ticket["cancelled"])
        self.assertEqual(state.get_snapshot()[1]["checkin_phase"], "cancelled")
        self.assertFalse(instance.request_checkin())

    def test_logout_also_clears_a_completed_but_not_displayed_result(self):
        instance, state = controller()
        instance.request_checkin()
        instance._finish_checkin(instance._checkin, "success")
        self.assertIsNone(instance._checkin)
        instance._cancel_checkin()
        self.assertEqual(state.get_snapshot()[1]["checkin_phase"], "cancelled")

    def test_released_key_at_deadline_does_not_rearm_one_ms_forever(self):
        worker = workers.LowPriorityWorker(checkin_request_callback=lambda: False)
        class EdgePort:
            batch_through_ms = 0
            def take_key_events(self):
                self.batch_through_ms = Clock.now
                events, self.events = self.events, ()
                return events
            def take_event_overflow(self):
                return 0
            def take_irq_diagnostics(self):
                return 0, None, None
        port = EdgePort()
        port.events = (("lamp", False, 1400),)
        worker.hardware_keys = port
        worker._key_stable_pressed["lamp"] = True
        worker._handle_lamp_key(keys.KEY_LAMP_PRESS, 0)
        Clock.now = 1500
        worker._handle_hardware_key_irq()
        self.assertIsNone(worker._lamp_pressed_at)
        self.assertFalse(worker._lamp_long_fired)

    def test_delayed_key_thread_preserves_two_short_volume_presses(self):
        class EdgePort:
            batch_through_ms = 500
            events = (("volume_up", True, 10), ("volume_up", False, 120),
                      ("volume_up", True, 220), ("volume_up", False, 330))
            def take_key_events(self):
                events, self.events = self.events, ()
                return events
            def take_event_overflow(self):
                return 0
            def take_irq_diagnostics(self):
                return 4, 10, 330
        actions = []
        worker = workers.LowPriorityWorker(key_action_callback=actions.append,
                                            hardware_keys=EdgePort())
        worker._handle_hardware_key_irq()
        self.assertEqual(actions, [keys.KEY_VOLUME_UP, keys.KEY_VOLUME_UP])
        self.assertIsNone(worker._lamp_pressed_at)

    def test_worker_dispatches_edges_without_a_second_debounce_wait(self):
        class EdgePort:
            batch_through_ms = 100
            events = (("lamp", True, 10), ("lamp", False, 20))
            def take_key_events(self):
                return self.events
            def take_event_overflow(self):
                return 0
            def take_irq_diagnostics(self):
                return 2, 10, 20
        worker = workers.LowPriorityWorker(hardware_keys=EdgePort())
        worker._toggle_lamp = Mock()
        worker._handle_hardware_key_irq()
        worker._toggle_lamp.assert_called_once_with("GPIO28短按松开")

    def test_ptt_press_and_release_survive_delayed_batch(self):
        class EdgePort:
            batch_through_ms = 500
            events = (("ptt", True, 10), ("ptt", False, 300))
            def take_key_events(self):
                return self.events
            def take_event_overflow(self):
                return 0
            def take_irq_diagnostics(self):
                return 2, 10, 300
        actions = []
        worker = workers.LowPriorityWorker(hardware_keys=EdgePort(),
                                            key_action_callback=actions.append)
        worker._handle_hardware_key_irq()
        self.assertEqual(actions, [keys.KEY_PTT_PRESS, keys.KEY_PTT_RELEASE])
        self.assertFalse(worker._key_stable_pressed["ptt"])

    def test_touch_busy_defers_edge_queue_until_ui_releases(self):
        class EdgePort:
            batch_through_ms = 100
            events = (("volume_down", True, 10),)
            def take_key_events(self):
                events, self.events = self.events, ()
                return events
            def has_pending_events(self):
                return bool(self.events)
        actions = []
        port = EdgePort()
        worker = workers.LowPriorityWorker(hardware_keys=port,
                                            key_action_callback=actions.append)
        worker.commands.set_ui_busy(True)
        worker._handle_hardware_key_irq()
        self.assertEqual(len(port.events), 1)
        self.assertEqual(actions, [])
        worker.commands.set_ui_busy(False)
        events = worker.commands.take_events()
        self.assertTrue(events[8])

    def test_gpio_irq_records_both_edges_without_reading_pin_in_callback(self):
        class FakePin:
            IN, PULL_PU = 0, 1
            GPIO5, GPIO28, GPIO29, GPIO30, GPIO31 = 5, 28, 29, 30, 31
            def __init__(self, gpio, *_args):
                self.gpio = gpio
                self.level = 1
                self.read_count = 0
            def read(self):
                self.read_count += 1
                return self.level

        class FakeExtInt:
            IRQ_RISING_FALLING, PULL_PU = 3, 1
            GPIO5, GPIO28, GPIO29, GPIO30, GPIO31 = 5, 28, 29, 30, 31
            instances = []
            def __init__(self, gpio, _mode, _pull, callback):
                self.gpio, self.callback = gpio, callback
                self.__class__.instances.append(self)
            def enable(self):
                pass
            def disable(self):
                pass

        previous_extint, previous_pin = keys.ExtInt, keys.Pin
        previous_micropython = keys.micropython
        FakeExtInt.instances = []
        keys.ExtInt, keys.Pin = FakeExtInt, FakePin
        scheduled = []
        notifications = []
        keys.micropython = types.SimpleNamespace(
            schedule=lambda callback, arg: scheduled.append((callback, arg)))
        try:
            Clock.now = 100
            port = keys.HardwareKeyPort(lambda: notifications.append(True))
            port.start()
            up_pin = port.volume_up_pin
            reads_after_start = up_pin.read_count
            irq = next(item for item in FakeExtInt.instances if item.gpio == 30)
            irq.callback((30, 1))
            Clock.now = 120
            irq.callback((30, 0))
            self.assertEqual(notifications, [])
            self.assertEqual(len(scheduled), 1)
            callback, arg = scheduled.pop()
            callback(arg)
            self.assertEqual(notifications, [True])
            self.assertEqual(up_pin.read_count, reads_after_start)
            self.assertEqual(port.take_key_events(), (
                ("volume_up", True, 100), ("volume_up", False, 120)))
            port.stop()
        finally:
            keys.ExtInt, keys.Pin = previous_extint, previous_pin
            keys.micropython = previous_micropython

    def test_isr_filters_same_key_edges_inside_twenty_ms(self):
        class FakePin:
            IN, PULL_PU = 0, 1
            GPIO5, GPIO28, GPIO29, GPIO30, GPIO31 = 5, 28, 29, 30, 31
            def __init__(self, gpio, *_args):
                self.gpio = gpio
            def read(self):
                return 1
        class FakeExtInt:
            IRQ_RISING_FALLING, PULL_PU = 3, 1
            GPIO5, GPIO28, GPIO29, GPIO30, GPIO31 = 5, 28, 29, 30, 31
            instances = []
            def __init__(self, gpio, _mode, _pull, callback):
                self.gpio, self.callback = gpio, callback
                self.__class__.instances.append(self)
            def enable(self):
                pass
            def disable(self):
                pass
        previous_extint, previous_pin = keys.ExtInt, keys.Pin
        FakeExtInt.instances = []
        keys.ExtInt, keys.Pin = FakeExtInt, FakePin
        try:
            Clock.now = 100
            port = keys.HardwareKeyPort(lambda: None)
            port.start()
            irq = next(item for item in FakeExtInt.instances if item.gpio == 30)
            irq.callback((30, 1))
            Clock.now = 119
            irq.callback((30, 0))
            self.assertEqual(port.take_key_events(), (("volume_up", True, 100),))
            Clock.now = 120
            irq.callback((30, 0))
            self.assertEqual(port.take_key_events(), (("volume_up", False, 120),))
            port.stop()
        finally:
            keys.ExtInt, keys.Pin = previous_extint, previous_pin

    def test_lamp_long_press_waits_for_touch_session_to_finish(self):
        class KeyPort:
            def is_key_pressed(self, _key_name):
                return True
        requests = []
        worker = workers.LowPriorityWorker(
            checkin_request_callback=lambda: requests.append(True) or True)
        worker.hardware_keys = KeyPort()
        worker._wake_screen = Mock()
        worker._lamp_pressed_at = 0
        worker._key_stable_pressed["lamp"] = True
        Clock.now = 1500
        worker.commands.set_ui_busy(True)
        self.assertFalse(worker._check_lamp_key_long())
        self.assertEqual(requests, [])
        worker.commands.set_ui_busy(False)
        self.assertTrue(worker._check_lamp_key_long())
        self.assertEqual(requests, [True])

    def test_missing_release_edge_recovers_before_next_long_press(self):
        class KeyPort:
            pressed = False
            def is_key_pressed(self, _key_name):
                return self.pressed
        requests = []
        port = KeyPort()
        worker = workers.LowPriorityWorker(
            checkin_request_callback=lambda: requests.append(True) or True)
        worker.hardware_keys = port
        worker._toggle_lamp = Mock()
        worker._lamp_pressed_at = 0
        worker._key_stable_pressed["lamp"] = True
        Clock.now = workers.LAMP_KEY_LONG_PRESS_MS
        self.assertFalse(worker._check_lamp_key_long())
        self.assertEqual(requests, [])
        Clock.now += workers.LAMP_KEY_CHECK_INTERVAL_MS
        self.assertFalse(worker._check_lamp_key_long())
        worker._toggle_lamp.assert_not_called()
        self.assertIsNone(worker._lamp_pressed_at)
        port.pressed = True
        worker._handle_lamp_key(keys.KEY_LAMP_PRESS, Clock.now)
        worker._lamp_pressed_at = utime.ticks_add(
            Clock.now, -workers.LAMP_KEY_LONG_PRESS_MS)
        self.assertTrue(worker._check_lamp_key_long())
        self.assertEqual(requests, [True])

    def test_missing_release_edge_recovers_short_press_once(self):
        class KeyPort:
            pressed = False
            def is_key_pressed(self, _key_name):
                return self.pressed
            def has_pending_events(self):
                return False
        port = KeyPort()
        worker = workers.LowPriorityWorker()
        worker.hardware_keys = port
        worker._toggle_lamp = Mock()
        worker._handle_lamp_key(keys.KEY_LAMP_PRESS, 100)
        Clock.now = 300
        self.assertFalse(worker._check_lamp_key_long())
        Clock.now += workers.LAMP_KEY_CHECK_INTERVAL_MS
        self.assertTrue(worker._check_lamp_key_long())
        worker._toggle_lamp.assert_called_once_with("GPIO28短按松开")
        self.assertIsNone(worker._lamp_pressed_at)
        self.assertFalse(worker._key_stable_pressed["lamp"])
        self.assertFalse(worker._check_lamp_key_long())
        worker._toggle_lamp.assert_called_once()

    def test_missing_release_edge_uses_first_release_observation_for_short_limit(self):
        class KeyPort:
            pressed = False
            def is_key_pressed(self, _key_name):
                return self.pressed
            def has_pending_events(self):
                return False
        worker = workers.LowPriorityWorker()
        worker.hardware_keys = KeyPort()
        worker._toggle_lamp = Mock()
        worker._handle_lamp_key(keys.KEY_LAMP_PRESS, 100)
        Clock.now = 100 + workers.LAMP_KEY_LONG_PRESS_MS - 50
        self.assertFalse(worker._check_lamp_key_long())
        Clock.now += workers.LAMP_KEY_CHECK_INTERVAL_MS
        self.assertTrue(worker._check_lamp_key_long())
        worker._toggle_lamp.assert_called_once_with("GPIO28短按松开")

if __name__ == "__main__":
    unittest.main()
