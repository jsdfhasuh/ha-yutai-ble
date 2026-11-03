"""Exercise SIGTERM-to-cancellation with no real signal or protocol imports."""

import asyncio
import importlib.util
import signal
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def load_worker():
    worker_path = Path(__file__).resolve().parents[1] / "custom_components" / "yutai_ble" / "protocol" / "bridge_worker.py"
    spec = importlib.util.spec_from_file_location("yutai_worker_signal_test", worker_path)
    module = importlib.util.module_from_spec(spec)
    dependencies = {name: types.ModuleType(name) for name in ("read_probe", "settings_codec", "settings_scan", "device_identity")}
    dependencies["device_identity"].ORDINARY_MODEL = "yutai.plug.fsov7m"
    dependencies["device_identity"].SUPPORTED_MODEL = "yutai.plug.fsov8m"
    with patch.dict(sys.modules, dependencies):
        spec.loader.exec_module(module)
    return module


worker = load_worker()


class WorkerSignalTests(unittest.IsolatedAsyncioTestCase):
    async def test_sigterm_callback_cancels_request_and_awaits_finally(self):
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        cleaned = asyncio.Event()
        registered = {}

        def install(signum, callback):
            registered[signum] = callback

        async def operation(*_):
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        with (
            patch.object(loop, "add_signal_handler", side_effect=install),
            patch.object(loop, "remove_signal_handler", return_value=True) as remove,
            patch.object(worker, "run_request", side_effect=operation),
        ):
            task = asyncio.create_task(worker.cancellable_request(b"synthetic", None))
            await started.wait()
            registered[signal.SIGTERM]()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(cleaned.is_set())
            remove.assert_called_once_with(signal.SIGTERM)

    async def test_normal_completion_removes_handler(self):
        loop = asyncio.get_running_loop()

        async def operation(*_):
            return {"status": "synthetic"}

        with (
            patch.object(loop, "add_signal_handler") as install,
            patch.object(loop, "remove_signal_handler", return_value=True) as remove,
            patch.object(worker, "run_request", side_effect=operation),
        ):
            result = await worker.cancellable_request(b"synthetic", None)
        self.assertEqual(result, {"status": "synthetic"})
        self.assertEqual(install.call_args.args[0], signal.SIGTERM)
        remove.assert_called_once_with(signal.SIGTERM)

    async def test_unsupported_signal_registration_preserves_request(self):
        loop = asyncio.get_running_loop()

        async def operation(*_):
            return {"status": "synthetic"}

        with (
            patch.object(loop, "add_signal_handler", side_effect=NotImplementedError),
            patch.object(loop, "remove_signal_handler") as remove,
            patch.object(worker, "run_request", side_effect=operation),
        ):
            result = await worker.cancellable_request(b"synthetic", None)
        self.assertEqual(result, {"status": "synthetic"})
        remove.assert_not_called()


if __name__ == "__main__":
    unittest.main()
