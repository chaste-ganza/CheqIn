students = {
    "03A70B2F": "Student 1",
    "11223344": "Student 2",
    "A1B2C3D4": "Student 3",
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

print("RFID ATTENDANCE SYSTEM")
print("Student identification test")
print("-" * 35)

identify_student("03A70B2F")
identify_student("FFFFFFFF")