from postgres_repository import PostgresTaskRepository


def main() -> None:
    repo = PostgresTaskRepository()
    repo.init_db()

    tasks = repo.list_tasks()
    print("initial_tasks", tasks)

    created = repo.create_task("Write integration tests")
    print("created", created)

    fetched = repo.get_task(created["id"])
    print("fetched", fetched)

    updated = repo.update_task(created["id"], None, True)
    print("updated", updated)

    deleted = repo.delete_task(created["id"])
    print("deleted", deleted)

    after_delete = repo.get_task(created["id"])
    print("after_delete", after_delete)

    assert after_delete is None


if __name__ == "__main__":
    main()