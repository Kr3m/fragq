import collections
import importlib.util
import logging
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import patch


FRAGQ = Path(__file__).resolve().parents[1] / "fragq.py"
RED = "76561190000000001"
BLUE = "76561190000000002"


def load_plugin(runtime):
    ql = types.ModuleType(runtime)

    class Plugin:
        def __init__(self):
            self.cvars = {"zmq_stats_enable": "0"}
            self.db = types.SimpleNamespace(get=lambda key: None)
            self.logger = logging.getLogger("fragq-test")
            self.hooks = {}

        def set_cvar_once(self, name, value):
            self.cvars.setdefault(name, value)

        def get_cvar(self, name):
            return self.cvars.get(name)

        def add_hook(self, name, handler, priority=None):
            gated = {"stats"} | ({"game_start", "game_end", "team_switch"} if runtime == "minqlx" else set())
            if name in gated and self.cvars["zmq_stats_enable"] == "0":
                raise AssertionError(f"{name} requires ZMQ")
            self.hooks[name] = handler

        def add_command(self, *args, **kwargs):
            pass

    ql.Plugin = Plugin
    ql.thread = lambda func: func
    ql.next_frame_tasks = collections.deque()

    def next_frame(func):
        def schedule(*args, **kwargs):
            ql.next_frame_tasks.append((func, args, kwargs))
        return schedule

    ql.next_frame = next_frame
    ql.RET_STOP_ALL = 3
    ql.PRI_HIGHEST = 0
    ql.Return = types.SimpleNamespace(STOP_ALL=3)
    ql.Priority = types.SimpleNamespace(HIGHEST=0)
    modules = {"minqlxtended": ql if runtime == "minqlxtended" else None, "minqlx": ql}
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location("fragq_voice_test", FRAGQ)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module, ql


class VoiceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.module, self.ql = load_plugin("minqlx")
        self.plugin = self.module.fragq.__new__(self.module.fragq)
        p = self.plugin
        p.logger = logging.getLogger("fragq-test")
        p._cfg = {"voice": True, "queue": "CA", "url": "https://example.invalid", "key": "key"}
        p._lock = threading.Lock()
        p._wake = threading.Event()
        p._voice_due = 0.0
        p._voice_out = collections.deque()
        p._voice_live = False
        p._voice_players = set()
        p._voice_generation = 0
        p._voice_team_switch_hooked = False
        p._voice_last = ""
        p._match = {}
        p._report = None
        p._finish_at = 0.0
        p.game = types.SimpleNamespace(state="countdown", type_short="ca")
        self.players = [types.SimpleNamespace(team="red", steam_id=RED),
                        types.SimpleNamespace(team="blue", steam_id=BLUE)]
        p.players = lambda: self.players
        p.load_config = lambda: p._cfg
        self.requests = []
        p._request = self.request
        p._reply = lambda *args: None

    def request(self, cfg, method, path, payload):
        self.requests.append((method, path, payload))
        return {"queue": "CA", "players": [{"status": "moved"}, {"status": "not_linked"}]}, None, 200

    def drain(self):
        while self.ql.next_frame_tasks:
            func, args, kwargs = self.ql.next_frame_tasks.popleft()
            func(*args, **kwargs)
        while self.plugin._voice_out:
            self.plugin._run_voice(self.plugin._cfg)

    def test_countdown_uses_same_payload_as_manual_and_respects_disabled(self):
        p = self.plugin
        p._cfg["voice"] = False
        p.handle_game_countdown()
        self.assertFalse(p._voice_out)
        p._cfg["voice"] = True
        p.handle_game_countdown()
        self.drain()
        p._manual_voice(None, p._voice_teams(), "teams")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0], self.requests[1])

    def test_round_countdown_moves_teams_using_configured_queue(self):
        p = self.plugin
        p._cfg["queue"] = "chi"
        p.game.state = "in_progress"
        p.handle_round_countdown(1)
        self.drain()
        self.assertEqual(self.requests, [("POST", "/voice", {
            "teams": {"red": [RED], "blue": [BLUE]}, "queue": "chi"
        })])
        self.assertTrue(p._voice_live)

    def test_round_countdown_without_match_countdown_returns_on_abort(self):
        p = self.plugin
        p._cfg["queue"] = "chi"
        p.game.state = "in_progress"
        p.handle_round_countdown(1)
        p.game.state = "warmup"
        p.handle_new_game()
        self.drain()
        self.assertEqual(set(self.requests[-1][2]["lobby"]), {RED, BLUE})
        self.assertEqual(self.requests[-1][2]["queue"], "chi")

    def test_round_countdown_without_match_countdown_returns_on_map_end(self):
        p = self.plugin
        p._cfg["queue"] = "chi"
        p.game.state = "in_progress"
        p.handle_round_countdown(1)
        p.handle_game_end(False)
        self.drain()
        self.assertEqual(set(self.requests[-1][2]["lobby"]), {RED, BLUE})
        self.assertEqual(self.requests[-1][2]["queue"], "chi")

    def test_abort_returns_tracked_players_including_disconnected(self):
        p = self.plugin
        p.handle_game_countdown()
        self.players.pop()
        p.game.state = "warmup"
        p.handle_new_game()
        self.drain()
        self.assertEqual(set(self.requests[-1][2]["lobby"]), {RED, BLUE})

    def test_map_does_not_discard_pending_return(self):
        p = self.plugin
        p.handle_game_countdown()
        p.handle_game_end()
        self.drain_frames()
        p.handle_map()
        self.drain()
        self.assertEqual([next(iter(req[2])) for req in self.requests], ["teams", "lobby"])
        self.assertEqual(set(self.requests[-1][2]["lobby"]), {RED, BLUE})

    def drain_frames(self):
        while self.ql.next_frame_tasks:
            func, args, kwargs = self.ql.next_frame_tasks.popleft()
            func(*args, **kwargs)

    def test_minqlx_intermission_returns_without_stats(self):
        p = self.plugin
        p.handle_game_countdown()
        p.handle_set_configstring(14, "1")
        self.drain()
        self.assertEqual(set(self.requests[-1][2]["lobby"]), {RED, BLUE})

    def test_stale_end_does_not_end_new_match(self):
        p = self.plugin
        p.handle_game_countdown()
        p.handle_game_end()
        p.game.state = "warmup"
        p.handle_new_game()
        p.game.state = "countdown"
        p.handle_game_countdown()
        self.drain()
        self.assertTrue(p._voice_live)
        self.assertEqual([next(iter(req[2])) for req in self.requests], ["teams", "lobby", "teams"])

    def test_return_is_split_into_valid_batches(self):
        p = self.plugin
        p.handle_game_countdown()
        p._voice_players.update(f"7656119{i:010d}" for i in range(100, 170))
        p.handle_new_game()  # still in countdown
        self.assertTrue(p._voice_live)
        p.game.state = "warmup"
        p.handle_new_game()
        self.drain()
        lobby = [req[2]["lobby"] for req in self.requests if "lobby" in req[2]]
        self.assertEqual([len(batch) for batch in lobby], [64, 8])
        self.assertEqual(len(set().union(*map(set, lobby))), 72)

    def test_queued_requests_keep_their_original_queue_configuration(self):
        p = self.plugin
        p.handle_game_countdown()
        p.game.state = "warmup"
        p.handle_new_game()
        p._cfg = dict(p._cfg, queue="Other", voice=False)
        self.drain()
        self.assertEqual([req[2]["queue"] for req in self.requests], ["CA", "CA"])

    def test_countdown_refreshes_cached_voice_setting(self):
        p = self.plugin
        p._cfg["voice"] = False

        def reload_config():
            p._cfg = dict(p._cfg, voice=True)
            return p._cfg

        p.load_config = reload_config
        p.handle_game_countdown()
        self.drain()
        self.assertIn("teams", self.requests[0][2])

    def test_old_team_snapshot_cannot_move_players_after_abort(self):
        p = self.plugin
        p.handle_game_countdown()
        p._voice_snapshot(p._voice_generation)
        p.game.state = "warmup"
        p.handle_new_game()
        self.drain()
        self.assertEqual([next(iter(req[2])) for req in self.requests], ["teams", "lobby"])

    def test_minqlx_team_attempt_schedules_update_without_zmq(self):
        p = self.plugin
        p.handle_game_countdown()
        p.game.state = "in_progress"
        p.handle_team_switch_attempt(self.players[0], "red", "blue")
        self.drain_frames()
        self.assertGreater(p._voice_due, 0)

    def test_worker_drains_team_and_lobby_requests_in_order(self):
        p = self.plugin
        p._stop = threading.Event()
        p._outbox = collections.deque()
        p._lookups = collections.deque()
        p._watch = {}
        p._finish_match = lambda cfg: None
        completed = threading.Event()

        def request(cfg, method, path, payload):
            result = self.request(cfg, method, path, payload)
            if len(self.requests) == 2:
                completed.set()
            return result

        p._request = request
        p.handle_game_countdown()
        p.game.state = "warmup"
        p.handle_new_game()
        worker = threading.Thread(target=p._worker)
        worker.start()
        try:
            self.assertTrue(completed.wait(2), "worker did not drain voice requests")
        finally:
            p._stop.set()
            p._wake.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual([next(iter(req[2])) for req in self.requests], ["teams", "lobby"])

    def test_plugin_loads_without_zmq_on_both_runtimes(self):
        for runtime in ("minqlx", "minqlxtended"):
            with self.subTest(runtime=runtime):
                module, _ = load_plugin(runtime)
                with patch.object(threading.Thread, "start"):
                    p = module.fragq()
                self.assertIn("game_countdown", p.hooks)
                self.assertIn("round_countdown", p.hooks)
                self.assertNotIn("stats", p.hooks)
                if runtime == "minqlx":
                    self.assertIn("set_configstring", p.hooks)
                    self.assertNotIn("game_end", p.hooks)
                else:
                    self.assertIn("game_end", p.hooks)

    def test_registered_round_hook_uses_current_cvars_in_both_runtimes(self):
        for runtime in ("minqlx", "minqlxtended"):
            with self.subTest(runtime=runtime):
                module, _ = load_plugin(runtime)
                with patch.object(threading.Thread, "start"):
                    p = module.fragq()
                p.cvars.update({"qlx_fragqVoice": "1", "qlx_fragqQueue": "chi",
                                "qlx_fragqUrl": "https://example.invalid", "qlx_fragqKey": "key"})
                p.game = types.SimpleNamespace(state="in_progress", type_short="ca")
                p.players = lambda: self.players
                p._request = self.request
                self.requests.clear()
                p.hooks["round_countdown"](1)
                p._run_voice(p._config())
                self.assertEqual(self.requests[0][2], {
                    "teams": {"red": [RED], "blue": [BLUE]}, "queue": "chi"
                })


if __name__ == "__main__":
    unittest.main()
