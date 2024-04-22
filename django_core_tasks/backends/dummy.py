from uuid import uuid4

from django_core_tasks.task import Task, TaskResult, TaskStatus

from .base import BaseTaskBackend


class DummyBackend(BaseTaskBackend):
    def __init__(self, options):
        super().__init__(options)

        self.results = []

    def enqueue(self, task: Task, args: list, kwargs: dict) -> TaskResult:
        self.validate_task(task)

        result = TaskResult(
            task=task,
            id=uuid4(),
            status=TaskStatus.NEW,
            args=args,
            kwargs=kwargs,
            backend=self.alias,
        )

        self.results.append(result)

        return result

    def clear(self):
        self.results.clear()
