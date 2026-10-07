import asyncio
import importlib
import json
import sys
import unittest
from unittest.mock import patch

from llm_d_bench.evaluate import harness_watch as watch


class HarnessWatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_network_error_is_not_hidden_by_time_window(self):
        async def read(env, *args):
            self.assertFalse(any(arg.startswith("--since") for arg in args))
            if "get" in args:
                return json.dumps({"items": [{"metadata": {"name": "harness"}, "status": {"phase": "Running"}}]})
            return "2026-09-14 11:53:44 Network is unreachable requesting HEAD https://huggingface.co/model/config.json"

        run = {}
        with patch.object(watch, "_read", read), self.assertRaisesRegex(RuntimeError, "Network is unreachable"):
            await watch.watch_harness(run, "ns", {}, lambda _: None, lambda: "now")
        self.assertIn("huggingface.co", run["harness_logs"])
        self.assertEqual(run["harness_checked_at"], "now")

    async def test_post_loadgen_analysis_failure_is_only_a_warning(self):
        warned = asyncio.Event()

        async def read(env, *args):
            if "get" in args:
                return json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": "harness"},
                                "status": {
                                    "phase": "Failed",
                                    "containerStatuses": [
                                        {"name": "harness", "state": {"terminated": {"exitCode": 137, "reason": "Error"}}}
                                    ],
                                },
                            }
                        ]
                    }
                )
            return "Harness completed successfully.\nRunning analysis: inference-perf-analyze_results.sh\n"

        run = {}

        def save(value):
            if value.get("harness_warning"):
                warned.set()

        with patch.object(watch, "_read", read), patch.object(watch, "_POLL_SECONDS", 0.02):
            task = asyncio.create_task(watch._poll_harness(run, "ns", {}, save, lambda: "now"))
            try:
                await asyncio.wait_for(warned.wait(), 1)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertIn("already completed", run["harness_warning"])

    async def test_loadgen_failure_without_the_completion_marker_still_raises(self):
        async def read(env, *args):
            if "get" in args:
                return json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": "harness"},
                                "status": {
                                    "phase": "Failed",
                                    "containerStatuses": [
                                        {"name": "harness", "state": {"terminated": {"exitCode": 1, "reason": "Error"}}}
                                    ],
                                },
                            }
                        ]
                    }
                )
            return "connection refused before any load was generated"

        with (
            patch.object(watch, "_read", read),
            patch.object(watch, "_POLL_SECONDS", 0.02),
            self.assertRaisesRegex(RuntimeError, r"harness: Error \(exit 1\)"),
        ):
            await asyncio.wait_for(
                watch._poll_harness({}, "ns", {}, lambda _: None, lambda: "now"), 1
            )

    def test_container_failure_detected_even_with_running_pod(self):
        self.assertIn(
            "OOMKilled",
            watch.pod_failure(
                {
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [
                            {"name": "harness", "state": {"terminated": {"exitCode": 137, "reason": "OOMKilled"}}}
                        ],
                    }
                }
            ),
        )
        self.assertIsNone(watch.pod_failure({"status": {"phase": "Succeeded"}}))

    async def test_watchdog_failure_terminates_runner(self):
        router = importlib.import_module("llm_d_bench.evaluate.router")

        async def fail(*args):
            await asyncio.sleep(0.05)
            raise RuntimeError("Benchmark harness failed: Network is unreachable")

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        with (
            patch.object(router, "watch_harness", fail),
            patch.object(router, "_save"),
            self.assertRaisesRegex(RuntimeError, "Network is unreachable"),
        ):
            await router._stream_benchmark_process(process, {"wait_timeout_seconds": 5}, namespace="ns")
        self.assertIsNotNone(process.returncode)


class HarnessSDKWatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_watch_failure_wakes_without_waiting_for_log_poll_and_closes(self):
        from llm_d_bench.utils import kubernetes_commands, kubernetes_watch

        polled = asyncio.Event()
        closed = asyncio.Event()
        reads = []

        async def read(environment, *args):
            reads.append(args)
            polled.set()
            return json.dumps({"items": []})

        async def events(*args, **kwargs):
            self.assertEqual(kwargs["namespace"], "ns")
            self.assertEqual(kwargs["selector"], "app=llmdbench-harness-launcher")
            try:
                await polled.wait()
                yield {
                    "type": "MODIFIED",
                    "object": {
                        "metadata": {"name": "harness", "uid": "one"},
                        "status": {"phase": "Failed", "reason": "WatchFailure"},
                    },
                }
                await asyncio.Event().wait()
            finally:
                closed.set()

        with (
            patch.object(kubernetes_commands, "sdk_enabled", return_value=True),
            patch.object(kubernetes_watch, "watch_resource_events", events),
            patch.object(watch, "_read", read),
            self.assertRaisesRegex(RuntimeError, "WatchFailure"),
        ):
            await asyncio.wait_for(watch.watch_harness({}, "ns", {}, lambda _: None, lambda: "now"), 1)
        self.assertTrue(closed.is_set())
        # One poll to list pods, plus one immediate log fetch to check whether the
        # load generator had already completed before failing.
        self.assertGreaterEqual(len(reads), 1)

    async def test_snapshot_reset_delete_and_cancellation(self):
        from llm_d_bench.utils import kubernetes_watch

        queue = asyncio.Queue()
        closed = asyncio.Event()
        live = {"pods": {}, "changed": asyncio.Event(), "warning": None}

        async def events(*args, **kwargs):
            try:
                while True:
                    yield await queue.get()
            finally:
                closed.set()

        async def send(kind, name="harness", uid=None):
            live["changed"].clear()
            metadata = {"name": name}
            if uid:
                metadata["uid"] = uid
            await queue.put({"type": kind, "object": {"metadata": metadata}})
            await asyncio.wait_for(live["changed"].wait(), 1)

        with patch.object(kubernetes_watch, "watch_resource_events", events):
            worker = asyncio.create_task(watch._watch_pods("ns", {}, live))
            try:
                await send("ADDED", uid="one")
                await send("MODIFIED", name="updated", uid="one")
                self.assertEqual(list(live["pods"]), ["one"])
                self.assertEqual(live["pods"]["one"]["metadata"]["name"], "updated")
                await send("DELETED", uid="one")
                self.assertEqual(live["pods"], {})
                await send("ADDED")
                self.assertIn("harness", live["pods"])
                await send("RESET")
                self.assertEqual(live["pods"], {})
            finally:
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await worker
        self.assertTrue(closed.is_set())

    async def test_watch_auth_error_sets_sanitized_warning(self):
        from kubernetes.aio.client.exceptions import ApiException

        from llm_d_bench.utils import kubernetes_commands, kubernetes_watch

        saved = asyncio.Event()
        run = {}

        async def read(*args):
            return json.dumps({"items": []})

        async def events(*args, **kwargs):
            raise ApiException(status=403, reason="secret-response")
            yield

        def save(value):
            if value.get("harness_warning"):
                saved.set()

        with (
            patch.object(kubernetes_commands, "sdk_enabled", return_value=True),
            patch.object(kubernetes_watch, "watch_resource_events", events),
            patch.object(watch, "_read", read),
        ):
            task = asyncio.create_task(watch.watch_harness(run, "ns", {}, save, lambda: "now"))
            try:
                await asyncio.wait_for(saved.wait(), 1)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(run["harness_warning"], "Cannot watch benchmark pods: ApiException")
        self.assertNotIn("secret", run["harness_warning"])

    async def test_cli_provider_keeps_polling_without_watch_warning(self):
        from llm_d_bench.utils import kubernetes_watch
        from llm_d_bench.utils.kubernetes_auth import CliAuthenticationRequiredError

        live = {"pods": {}, "changed": asyncio.Event(), "warning": None}

        async def events(*args, **kwargs):
            raise CliAuthenticationRequiredError
            yield

        with patch.object(kubernetes_watch, "watch_resource_events", events):
            await watch._watch_pods("ns", {}, live)
        self.assertIsNone(live["warning"])
        self.assertFalse(live["changed"].is_set())

    async def test_periodic_logs_continue_and_cancellation_closes_watch(self):
        from llm_d_bench.utils import kubernetes_commands, kubernetes_watch

        refreshed = asyncio.Event()
        closed = asyncio.Event()
        logs = []

        async def read(environment, *args):
            if "get" in args:
                return json.dumps({"items": [{"metadata": {"name": "harness"}, "status": {"phase": "Running"}}]})
            logs.append(args)
            if len(logs) >= 2:
                refreshed.set()
            return "benchmark progress"

        async def events(*args, **kwargs):
            try:
                await asyncio.Event().wait()
                yield
            finally:
                closed.set()

        with (
            patch.object(kubernetes_commands, "sdk_enabled", return_value=True),
            patch.object(kubernetes_watch, "watch_resource_events", events),
            patch.object(watch, "_read", read),
            patch.object(watch, "_POLL_SECONDS", 0.02),
        ):
            task = asyncio.create_task(watch.watch_harness({}, "ns", {}, lambda _: None, lambda: "now"))
            try:
                await asyncio.wait_for(refreshed.wait(), 1)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertGreaterEqual(len(logs), 2)
        self.assertTrue(closed.is_set())
