from abc import ABC, abstractmethod


class TaskRepository(ABC):
    @abstractmethod
    def init_db(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def list_tasks(self) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def get_task(self, task_id: int) -> dict | None:
        raise NotImplementedError

    @abstractmethod
    def create_task(self, title: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    def update_task(self, task_id: int, title: str | None, done: bool | None) -> dict | None:
        raise NotImplementedError

    @abstractmethod
    def delete_task(self, task_id: int) -> bool:
        raise NotImplementedError