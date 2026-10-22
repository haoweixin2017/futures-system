"""启动依赖检查隔离测试，不启动服务、不访问数据库。"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts import startup_dependencies as startup


class StartupDependenciesTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(
            mysql=SimpleNamespace(host='127.0.0.1', port=3306),
            redis=SimpleNamespace(host='localhost', port=6379))

    @patch.object(startup, 'start_windows_service')
    @patch.object(startup, 'port_ready', return_value=True)
    def test_running_dependencies_are_untouched(self, probe, start):
        startup.ensure_dependencies(self.config)
        start.assert_not_called()

    @patch.object(startup, 'start_windows_service')
    @patch.object(startup, 'port_ready', side_effect=[True, False, True])
    def test_stopped_local_redis_is_started_and_rechecked(self, probe, start):
        startup.ensure_dependencies(self.config)
        start.assert_called_once_with('Redis')
        self.assertEqual(probe.call_count, 3)

    @patch.object(startup, 'start_windows_service')
    @patch.object(startup, 'port_ready', return_value=False)
    def test_remote_dependency_never_starts_local_service(self, probe, start):
        self.config.mysql.host = 'db.example.test'
        with self.assertRaisesRegex(RuntimeError, '远程'):
            startup.ensure_dependencies(self.config)
        start.assert_not_called()

    @patch.object(startup, 'start_windows_service', side_effect=RuntimeError('拒绝授权'))
    @patch.object(startup, 'port_ready', return_value=False)
    def test_start_failure_is_not_hidden(self, probe, start):
        with self.assertRaisesRegex(RuntimeError, '拒绝授权'):
            startup.ensure_dependencies(self.config)

    @patch.object(startup.time, 'sleep')
    @patch.object(startup.time, 'monotonic', side_effect=[0, 61])
    @patch.object(startup, 'start_windows_service')
    @patch.object(startup, 'port_ready', return_value=False)
    def test_service_without_listener_times_out(self, probe, start, timer, sleep):
        with self.assertRaisesRegex(RuntimeError, '3306'):
            startup.ensure_dependencies(self.config)

    @patch.object(startup.subprocess, 'run')
    def test_service_script_only_elevates_service_start(self, run):
        run.return_value = SimpleNamespace(returncode=0)
        startup.start_windows_service('Redis')
        script = run.call_args.args[0][-1]
        self.assertIn('Start-Service', script)
        self.assertIn('-Verb RunAs', script)
        self.assertNotIn('run.py', script)

    @patch.object(startup.subprocess, 'run')
    def test_declined_elevation_stops_startup(self, run):
        run.return_value = SimpleNamespace(returncode=1)
        with self.assertRaisesRegex(RuntimeError, 'Start-Service Redis'):
            startup.start_windows_service('Redis')

    @patch.object(startup.subprocess, 'run', side_effect=startup.subprocess.TimeoutExpired('powershell', 120))
    def test_elevation_wait_is_bounded(self, run):
        with self.assertRaisesRegex(RuntimeError, '超时'):
            startup.start_windows_service('Redis')


if __name__ == '__main__':
    unittest.main()
