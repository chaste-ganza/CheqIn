import serial
import time
import csv
import os
import sqlite3
from datetime import datetime

SERIAL_PORT = "COM7"
BAUD_RATE = 115200

ATTENDANCE_FILE = "attendance.csv"

DATABASE = "attendance.db"




def get_student(card_uid):
    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute(
        "SELECT id, name FROM students WHERE card_uid = ?",
        (card_uid,)
    )

    student = cursor.fetchone()

    connection.close()

    return student



def create_attendance_file():

    if not os.path.exists(ATTENDANCE_FILE):
        with open(ATTENDANCE_FILE, "w", newline="") as file:
            writer = csv.writer(file)

            writer.writerow([
                "Student Name",
                "Card UID",
                "Date",
                "Time"
            ])
        print("Attendance file created")


def already_attended(card_uid, today):
    if not os.path.exists(ATTENDANCE_FILE):
        return False

    with open(ATTENDANCE_FILE, "r", newline="") as file:
        reader = csv.DictReader(file)

        for row in reader:
            if (
                row["Card UID"] == card_uid
                and row["Date"] == today
            ):
                return True

    return False


def identify_student(card_uid):
    card_uid = card_uid.strip().replace(" ", "").upper()

    student = get_student(card_uid)

    if student is None:
        print("\nUNKOWN CARD!")
        print(f"Card UID: {card_uid}")
        print("This card is not registered.")
        print("-" * 35)
        return

    student_id = student[0]
    student_name = student[1]
    now = datetime.now()

    date = now.strftime("%Y-%m-%d")
    time_scanned = now.strftime("%H:%M:%S")

    if already_attended(card_uid, date):
        print("\nARLEADY MARKED!")
        print(f"Name: {student_name}")
        print(f"Date: {date}")
        print("Attendance has already been recorded today.")
        print("-" * 35)
        return

    print("\nSTUDENT IDENTIFIED!")
    print(f"Name: {student_name}")
    print(f"Card UID: {card_uid}")
    print(f"Time: {time_scanned}")

    with open(ATTENDANCE_FILE, "a", newline="") as file:
        writer = csv.writer(file)

        writer.writerow([
            student_name,
            card_uid,
            date,
            time_scanned
        ])

    print("Attendance recorded successfully!")
    print("-" * 35)


def main():
    create_attendance_file()

    try:
        print("Connnecting to RFID reader...")

        with serial.Serial(
            SERIAL_PORT,
            BAUD_RATE,
            timeout=1
        ) as esp:
            time.sleep(2)
            esp.reset_input_buffer()

            print("Connnect to ESP8266!")
            esp.write(b"ON\n")

            print("RFID system is starting")
            print("Tap a registered RFID card.")
            print("Press Ctrl+C to stop.\n")

            while True:
                line = esp.readline().decode(
                    "utf-8", errors="ignore"
                ).strip()

                if not line:
                    continue

                print(f"[ESP8266] {line}")

                if line.startswith("UID:"):
                    card_uid = line.split(":", 1)[1].strip()
                    identify_student(card_uid)
    except serial.SerialException as error:
        print("\nCould not connect to the ESP8266.")
        print("Check the COM port and USB connection")
        print(f"Details: {error}")

    except KeyboardInterrupt:
        print("\nRFID application stopped.")

if __name__ == "__main__":
    main()