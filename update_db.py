import database


if __name__ == "__main__":
    database.initialize_database()
    print(
        f"Database ready at schema version {database.LATEST_SCHEMA_VERSION} "
        f"({database.DB_PATH})"
    )
