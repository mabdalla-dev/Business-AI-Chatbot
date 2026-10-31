from db import DATABASE_PATH, init_db


if __name__ == "__main__":
    init_db()

    print("Database initialized successfully.")
    print(f"Database location: {DATABASE_PATH}")