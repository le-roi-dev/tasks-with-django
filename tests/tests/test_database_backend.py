import json
import uuid
from contextlib import redirect_stderr
from datetime import timedelta
from functools import partial
from io import StringIO

from django.core.exceptions import SuspiciousOperation
from django.core.management import call_command, execute_from_command_line
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from django_tasks import ResultStatus, default_task_backend, tasks
from django_tasks.backends.database import DatabaseBackend
from django_tasks.backends.database.management.commands.db_worker import (
    logger as db_worker_logger,
)
from django_tasks.backends.database.models import DBTaskResult
from django_tasks.exceptions import ResultDoesNotExist
from tests import tasks as test_tasks


@override_settings(
    TASKS={"default": {"BACKEND": "django_tasks.backends.database.DatabaseBackend"}}
)
class DatabaseBackendTestCase(TestCase):
    def test_using_correct_backend(self) -> None:
        self.assertEqual(default_task_backend, tasks["default"])
        self.assertIsInstance(tasks["default"], DatabaseBackend)

    def test_enqueue_task(self) -> None:
        for task in [test_tasks.noop_task, test_tasks.noop_task_async]:
            with self.subTest(task), self.assertNumQueries(1):
                result = default_task_backend.enqueue(task, (1,), {"two": 3})

                self.assertEqual(result.status, ResultStatus.NEW)
                self.assertIsNone(result.finished_at)
                with self.assertRaisesMessage(ValueError, "Task has not finished yet"):
                    result.result  # noqa:B018
                self.assertEqual(result.task, task)
                self.assertEqual(result.args, [1])
                self.assertEqual(result.kwargs, {"two": 3})

    async def test_enqueue_task_async(self) -> None:
        for task in [test_tasks.noop_task, test_tasks.noop_task_async]:
            with self.subTest(task):
                result = await default_task_backend.aenqueue(task, [], {})

                self.assertEqual(result.status, ResultStatus.NEW)
                self.assertIsNone(result.finished_at)
                with self.assertRaisesMessage(ValueError, "Task has not finished yet"):
                    result.result  # noqa:B018
                self.assertEqual(result.task, task)
                self.assertEqual(result.args, [])
                self.assertEqual(result.kwargs, {})

    def test_get_result(self) -> None:
        with self.assertNumQueries(1):
            result = default_task_backend.enqueue(test_tasks.noop_task, [], {})

        with self.assertNumQueries(1):
            new_result = default_task_backend.get_result(result.id)

        self.assertEqual(result, new_result)

    async def test_get_result_async(self) -> None:
        result = await default_task_backend.aenqueue(test_tasks.noop_task, [], {})

        new_result = await default_task_backend.aget_result(result.id)

        self.assertEqual(result, new_result)

    def test_refresh_result(self) -> None:
        result = default_task_backend.enqueue(
            test_tasks.calculate_meaning_of_life, (), {}
        )

        DBTaskResult.objects.all().update(
            status=ResultStatus.COMPLETE, finished_at=timezone.now()
        )

        self.assertEqual(result.status, ResultStatus.NEW)
        self.assertIsNone(result.finished_at)
        with self.assertNumQueries(1):
            result.refresh()
        self.assertIsNotNone(result.finished_at)
        self.assertEqual(result.status, ResultStatus.COMPLETE)

    async def test_refresh_result_async(self) -> None:
        result = await default_task_backend.aenqueue(
            test_tasks.calculate_meaning_of_life, (), {}
        )

        await DBTaskResult.objects.all().aupdate(
            status=ResultStatus.COMPLETE, finished_at=timezone.now()
        )

        self.assertEqual(result.status, ResultStatus.NEW)
        self.assertIsNone(result.finished_at)
        await result.arefresh()
        self.assertIsNotNone(result.finished_at)
        self.assertEqual(result.status, ResultStatus.COMPLETE)

    def test_get_missing_result(self) -> None:
        with self.assertRaises(ResultDoesNotExist):
            default_task_backend.get_result(uuid.uuid4())

    async def test_async_get_missing_result(self) -> None:
        with self.assertRaises(ResultDoesNotExist):
            await default_task_backend.aget_result(uuid.uuid4())

    def test_invalid_uuid(self) -> None:
        with self.assertRaises(ResultDoesNotExist):
            default_task_backend.get_result("123")

    async def test_async_invalid_uuid(self) -> None:
        with self.assertRaises(ResultDoesNotExist):
            await default_task_backend.aget_result("123")

    def test_meaning_of_life_view(self) -> None:
        for url in [
            reverse("meaning-of-life"),
            reverse("meaning-of-life-async"),
        ]:
            with self.subTest(url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)

                data = json.loads(response.content)

                self.assertEqual(data["result"], None)
                self.assertEqual(data["status"], ResultStatus.NEW)

                result = default_task_backend.get_result(data["result_id"])
                self.assertEqual(result.status, ResultStatus.NEW)

    def test_get_result_from_different_request(self) -> None:
        response = self.client.get(reverse("meaning-of-life"))
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.content)
        result_id = data["result_id"]

        response = self.client.get(reverse("result", args=[result_id]))
        self.assertEqual(response.status_code, 200)

        self.assertEqual(
            json.loads(response.content),
            {"result_id": result_id, "result": None, "status": ResultStatus.NEW},
        )

    def test_invalid_task_path(self) -> None:
        db_task_result = DBTaskResult.objects.create(
            args_kwargs={"args": [["exit", "1"]], "kwargs": {}},
            task_path="subprocess.check_output",
            backend_name="default",
        )

        with self.assertRaisesMessage(
            SuspiciousOperation,
            f"Task {db_task_result.id} does not point to a Task ({db_task_result.task_path})",
        ):
            _ = db_task_result.task

    def test_missing_task_path(self) -> None:
        db_task_result = DBTaskResult.objects.create(
            args_kwargs={"args": [], "kwargs": {}},
            task_path="missing.func",
            backend_name="default",
        )

        with self.assertRaises(
            ImportError,
        ):
            _ = db_task_result.task


@override_settings(
    TASKS={
        "default": {"BACKEND": "django_tasks.backends.database.DatabaseBackend"},
        "dummy": {"BACKEND": "django_tasks.backends.dummy.DummyBackend"},
    }
)
class DatabaseBackendWorkerTestCase(TransactionTestCase):
    run_worker = partial(call_command, "db_worker", verbosity=0, batch=True, interval=0)

    def tearDown(self) -> None:
        # Reset the logger after every run, to ensure the correct `stdout` is used
        for handler in db_worker_logger.handlers:
            db_worker_logger.removeHandler(handler)

    def test_run_enqueued_task(self) -> None:
        for task in [
            test_tasks.noop_task,
            test_tasks.noop_task_async,
        ]:
            with self.subTest(task):
                result = default_task_backend.enqueue(task, [], {})
                self.assertEqual(DBTaskResult.objects.ready().count(), 1)

                self.assertEqual(result.status, ResultStatus.NEW)

                with self.assertNumQueries(8):
                    self.run_worker()

                self.assertEqual(result.status, ResultStatus.NEW)
                result.refresh()
                self.assertIsNotNone(result.finished_at)
                self.assertGreaterEqual(result.finished_at, result.enqueued_at)
                self.assertEqual(result.status, ResultStatus.COMPLETE)

                self.assertEqual(DBTaskResult.objects.ready().count(), 0)

    def test_batch_processes_all_tasks(self) -> None:
        for _ in range(3):
            test_tasks.noop_task.enqueue()
        test_tasks.failing_task.enqueue()

        self.assertEqual(DBTaskResult.objects.ready().count(), 4)

        with self.assertNumQueries(23):
            self.run_worker()

        self.assertEqual(DBTaskResult.objects.ready().count(), 0)
        self.assertEqual(DBTaskResult.objects.complete().count(), 3)
        self.assertEqual(DBTaskResult.objects.failed().count(), 1)

    def test_no_tasks(self) -> None:
        with self.assertNumQueries(3):
            self.run_worker()

    def test_doesnt_process_different_queue(self) -> None:
        result = test_tasks.noop_task.using(queue_name="queue-1").enqueue()

        self.assertEqual(DBTaskResult.objects.ready().count(), 1)

        with self.assertNumQueries(3):
            self.run_worker()

        self.assertEqual(DBTaskResult.objects.ready().count(), 1)

        with self.assertNumQueries(8):
            self.run_worker(queue_name=result.task.queue_name)

        self.assertEqual(DBTaskResult.objects.ready().count(), 0)

    def test_process_all_queues(self) -> None:
        test_tasks.noop_task.using(queue_name="queue-1").enqueue()

        self.assertEqual(DBTaskResult.objects.ready().count(), 1)

        with self.assertNumQueries(3):
            self.run_worker()

        self.assertEqual(DBTaskResult.objects.ready().count(), 1)

        with self.assertNumQueries(8):
            self.run_worker(queue_name="*")

        self.assertEqual(DBTaskResult.objects.ready().count(), 0)

    def test_failing_task(self) -> None:
        result = test_tasks.failing_task.enqueue()
        self.assertEqual(DBTaskResult.objects.ready().count(), 1)

        with self.assertNumQueries(8):
            self.run_worker()

        self.assertEqual(result.status, ResultStatus.NEW)
        result.refresh()
        self.assertIsNotNone(result.finished_at)
        self.assertGreaterEqual(result.finished_at, result.enqueued_at)  # type: ignore[arg-type]
        self.assertEqual(result.status, ResultStatus.FAILED)

        self.assertEqual(DBTaskResult.objects.ready().count(), 0)

    def test_doesnt_process_different_backend(self) -> None:
        result = test_tasks.failing_task.enqueue()

        self.assertEqual(DBTaskResult.objects.ready().count(), 1)

        with self.assertNumQueries(3):
            self.run_worker(backend_name="dummy")

        self.assertEqual(DBTaskResult.objects.ready().count(), 1)

        with self.assertNumQueries(8):
            self.run_worker(backend_name=result.backend)

        self.assertEqual(DBTaskResult.objects.ready().count(), 0)

    def test_unknown_backend(self) -> None:
        output = StringIO()
        with redirect_stderr(output):
            with self.assertRaises(SystemExit):
                execute_from_command_line(
                    ["django-admin", "db_worker", "--backend", "unknown"]
                )
        self.assertIn("The connection 'unknown' doesn't exist.", output.getvalue())

    def test_negative_interval(self) -> None:
        output = StringIO()
        with redirect_stderr(output):
            with self.assertRaises(SystemExit):
                execute_from_command_line(
                    ["django-admin", "db_worker", "--interval", "-1"]
                )
        self.assertIn("Must be greater than zero", output.getvalue())

    def test_infinite_interval(self) -> None:
        output = StringIO()
        with redirect_stderr(output):
            with self.assertRaises(SystemExit):
                execute_from_command_line(
                    ["django-admin", "db_worker", "--interval", "inf"]
                )
        self.assertIn("invalid valid_interval value: 'inf'", output.getvalue())

    def test_run_after(self) -> None:
        result = test_tasks.noop_task.using(
            run_after=timezone.now() + timedelta(hours=10)
        ).enqueue()

        self.assertEqual(DBTaskResult.objects.count(), 1)
        self.assertEqual(DBTaskResult.objects.ready().count(), 0)

        with self.assertNumQueries(3):
            self.run_worker()

        self.assertEqual(DBTaskResult.objects.count(), 1)
        self.assertEqual(DBTaskResult.objects.ready().count(), 0)
        self.assertEqual(DBTaskResult.objects.complete().count(), 0)

        DBTaskResult.objects.filter(id=result.id).update(run_after=timezone.now())

        self.assertEqual(DBTaskResult.objects.ready().count(), 1)

        with self.assertNumQueries(8):
            self.run_worker()

        self.assertEqual(DBTaskResult.objects.ready().count(), 0)
        self.assertEqual(DBTaskResult.objects.complete().count(), 1)

    def test_run_after_priority(self) -> None:
        far_future_result = test_tasks.noop_task.using(
            run_after=timezone.now() + timedelta(hours=10)
        ).enqueue()

        high_priority_far_future_result = test_tasks.noop_task.using(
            priority=10, run_after=timezone.now() + timedelta(hours=10)
        ).enqueue()

        future_result = test_tasks.noop_task.using(
            run_after=timezone.now() + timedelta(hours=2)
        ).enqueue()

        high_priority_result = test_tasks.noop_task.using(priority=10).enqueue()

        low_priority_result = test_tasks.noop_task.using(priority=2).enqueue()

        self.assertEqual(
            [dbt.task_result for dbt in DBTaskResult.objects.all()],
            [
                high_priority_far_future_result,
                high_priority_result,
                low_priority_result,
                far_future_result,
                future_result,
            ],
        )

        self.assertEqual(
            [dbt.task_result for dbt in DBTaskResult.objects.ready()],
            [
                high_priority_result,
                low_priority_result,
            ],
        )

    def test_verbose_logging(self) -> None:
        result = test_tasks.noop_task.enqueue()

        stdout = StringIO()
        self.run_worker(verbosity=3, stdout=stdout, stderr=stdout)

        self.assertEqual(
            stdout.getvalue().splitlines(),
            [
                "Starting worker for queues=default",
                f"Task id={result.id} path=tests.tasks.noop_task state=RUNNING",
                f"Task id={result.id} path=tests.tasks.noop_task state=COMPLETE",
                "No more tasks to run - exiting gracefully.",
            ],
        )

    def test_invalid_task_path(self) -> None:
        db_task_result = DBTaskResult.objects.create(
            args_kwargs={"args": [["exit", "1"]], "kwargs": {}},
            task_path="subprocess.check_output",
            backend_name="default",
        )

        self.run_worker()

        db_task_result.refresh_from_db()

        self.assertEqual(db_task_result.status, ResultStatus.FAILED)

    def test_missing_task_path(self) -> None:
        db_task_result = DBTaskResult.objects.create(
            args_kwargs={"args": [], "kwargs": {}},
            task_path="missing.func",
            backend_name="default",
        )

        self.run_worker()

        db_task_result.refresh_from_db()

        self.assertEqual(db_task_result.status, ResultStatus.FAILED)

    def test_worker_doesnt_exit(self) -> None:
        result = test_tasks.exit_task.enqueue()

        self.run_worker()

        result.refresh()
        self.assertEqual(result.status, ResultStatus.FAILED)
