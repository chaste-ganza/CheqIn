import serial
import time

SERIAL_PORT = "COM7"
BAUD_RATE = 115200

students = {
    "A96E9504": "Student 1",
    "AB529E04": "Student 2",
    "594AB9D4": "Student 3",
}

def identify_student(card_uid):
    card_uid = card_uid.strip().replace(" ", "").upper()

    if card_uid in students:
        student_name = students[card_uid]

        print("\nSTUDENT IDENTIFIED!")
        print(f"Name: {student_name}")
        print(f"Card UID: {card_uid}")
    else:
        print("\nUNKNOWN CARD!")
        print(f"Card UID: {card_uid}")
        print("This card is not registered.")

    print("-" * 35)

try:
    print("Connecting to RFID reader...")

    with serial.Serial(
        SERIAL_PORT,
        BAUD_RATE,
        timeout=1
    ) as esp:
        time.sleep(2)

        esp.reset_input_buffer()

        print("Connected to ESP8266!")
        print("Sending ON command...")

        esp.write(b"ON\n")

        print("RFID system is starting.")
        print("Tap an RFID card to identify a student.")
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
    print("Check the COM port and USB connection.")
    print(f"Details: {error}")

except KeyboardInterrupt:
    print("\nRFID application stopped")