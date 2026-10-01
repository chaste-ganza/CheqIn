import sqlite3

DATABASE = "attendance.db"

def create_database():
    connection = sqlite3.connect(DATABASE)

    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            card_uid TEXT UNIQUE NOT NULL
        )
    """)

    students = [
        ("Student 1", "A96E9504"),
        ("Student 2", "AB529E04"),
        ("Student 3", "594AB9D4")
    ]

    cursor.executemany("""
    INSERT OR IGNORE INTO students (name, card_uid)
    VALUES (?, ?)
    """, students)

    connection.commit()
    connection.close()

    print("Database created successfully!")
    print("Students registered successfully!")

create_database()