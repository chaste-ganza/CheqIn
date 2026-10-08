/*
 * rfid_reader.ino
 * ---------------
 * ESP8266 + MFRC522 RFID Reader Firmware
 *
 * Hardware:
 *   Board  : NodeMCU ESP8266
 *   Reader : MFRC522 RC522
 *
 * Pin mapping:
 *   SS_PIN   (SDA/CS) -> D2  (GPIO 4)
 *   RST_PIN           -> D1  (GPIO 5)
 *   MOSI              -> D7  (GPIO 13)  [SPI - fixed]
 *   MISO              -> D6  (GPIO 12)  [SPI - fixed]
 *   SCK               -> D5  (GPIO 14)  [SPI - fixed]
 *   RED_LED           -> D4  (GPIO 2)
 *   GREEN_LED         -> D3  (GPIO 0)
 *   CARD_LED          -> D0  (GPIO 16)
 *
 * Serial: 115200 baud
 *
 * Commands received from Python (newline-terminated):
 *
 *   ON     -> Enable RFID scanning (normal attendance mode)
 *   OFF    -> Disable RFID scanning
 *   STATUS -> Report current state
 *   ASSIGN -> Scan exactly one card in assignment mode, then return to normal
 *
 * Output sent to Python:
 *
 *   SYSTEM: ON              (response to ON command)
 *   RFID reader: ACTIVE     (response to ON command, second line)
 *   SYSTEM: OFF             (response to OFF command)
 *   RFID reader: INACTIVE   (response to OFF command, second line)
 *   STATUS: ON              (response to STATUS when active)
 *   STATUS: OFF             (response to STATUS when inactive)
 *   ASSIGN: WAITING         (response to ASSIGN, reader now waiting for card)
 *   UID: XXXXXXXX           (card scanned - both NORMAL and ASSIGN mode)
 *   ASSIGN: DONE            (after one card scanned in ASSIGN mode)
 *   ERROR: ...              (malformed or unexpected input)
 *
 * Modes:
 *   MODE_OFF    - reader disabled, ignores cards
 *   MODE_NORMAL - reader active, every card produces UID: output
 *   MODE_ASSIGN - reader waits for exactly one card, then returns to MODE_NORMAL
 *
 * The Python layer differentiates NORMAL vs ASSIGN events by tracking
 * which mode it requested. The firmware sends the same UID: format in
 * both modes so the existing Python UID parsing is unchanged.
 * The ASSIGN: DONE line signals that assignment mode has ended.
 *
 * Libraries required (install via Arduino Library Manager):
 *   MFRC522  by GithubCommunity  (search: "MFRC522")
 */

#include <SPI.h>
#include <MFRC522.h>

// ---------------------------------------------------------------------------
// Pin definitions
// ---------------------------------------------------------------------------

#define SS_PIN    4    // D2
#define RST_PIN   5    // D1
#define RED_LED   2    // D4  (active LOW on NodeMCU)
#define GREEN_LED 0    // D3  (active LOW on NodeMCU)
#define CARD_LED  16   // D0  (active HIGH)

// ---------------------------------------------------------------------------
// RFID reader instance
// ---------------------------------------------------------------------------

MFRC522 mfrc522(SS_PIN, RST_PIN);

// ---------------------------------------------------------------------------
// Firmware state
// ---------------------------------------------------------------------------

typedef enum {
    MODE_OFF    = 0,
    MODE_NORMAL = 1,
    MODE_ASSIGN = 2
} ReaderMode;

ReaderMode currentMode = MODE_OFF;

// Debounce: minimum milliseconds between two card reads.
// Prevents the same card being read multiple times in one tap.
const unsigned long DEBOUNCE_MS = 1500;
unsigned long lastReadTime = 0;

// ---------------------------------------------------------------------------
// LED helpers
// ---------------------------------------------------------------------------

void setLeds(bool red, bool green, bool card) {
    // RED and GREEN are active-LOW on NodeMCU.
    digitalWrite(RED_LED,   red   ? LOW  : HIGH);
    digitalWrite(GREEN_LED, green ? LOW  : HIGH);
    digitalWrite(CARD_LED,  card  ? HIGH : LOW);
}

void ledIdle() {
    // System on, waiting: green steady, others off.
    setLeds(false, true, false);
}

void ledOff() {
    // System off: red steady, others off.
    setLeds(true, false, false);
}

void ledAssignWaiting() {
    // Assignment mode waiting: both on (amber effect), card off.
    setLeds(true, true, false);
}

void ledCardFlash() {
    // Card just read: card LED on, green off briefly, then restore.
    setLeds(false, false, true);
    delay(300);
    ledIdle();
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

void setup() {
    Serial.begin(115200);

    pinMode(RED_LED,   OUTPUT);
    pinMode(GREEN_LED, OUTPUT);
    pinMode(CARD_LED,  OUTPUT);

    SPI.begin();
    mfrc522.PCD_Init();

    // Start in OFF state.
    ledOff();

    Serial.println("BOOT: RFID reader ready");
    Serial.println("BOOT: Waiting for commands");
}

// ---------------------------------------------------------------------------
// UID extraction helper
// ---------------------------------------------------------------------------

String readUID() {
    String uid = "";
    for (byte i = 0; i < mfrc522.uid.size; i++) {
        if (mfrc522.uid.uidByte[i] < 0x10) {
            uid += "0";
        }
        uid += String(mfrc522.uid.uidByte[i], HEX);
    }
    uid.toUpperCase();
    return uid;
}

// ---------------------------------------------------------------------------
// Command handler
// ---------------------------------------------------------------------------

void handleCommand(String cmd) {
    cmd.trim();
    cmd.toUpperCase();

    if (cmd == "ON") {
        currentMode = MODE_NORMAL;
        ledIdle();
        Serial.println("SYSTEM: ON");
        Serial.println("RFID reader: ACTIVE");

    } else if (cmd == "OFF") {
        currentMode = MODE_OFF;
        ledOff();
        Serial.println("SYSTEM: OFF");
        Serial.println("RFID reader: INACTIVE");

    } else if (cmd == "STATUS") {
        if (currentMode == MODE_OFF) {
            Serial.println("STATUS: OFF");
        } else {
            // Both NORMAL and ASSIGN count as "on" for STATUS purposes.
            Serial.println("STATUS: ON");
        }

    } else if (cmd == "ASSIGN") {
        if (currentMode == MODE_OFF) {
            // Cannot enter assign mode while the reader is off.
            Serial.println("ERROR: Cannot enter ASSIGN mode while system is OFF. Send ON first.");
        } else if (currentMode == MODE_ASSIGN) {
            // Already waiting - ignore duplicate ASSIGN command.
            Serial.println("ERROR: Already in ASSIGN mode. Waiting for card.");
        } else {
            // Switch from NORMAL to ASSIGN.
            currentMode = MODE_ASSIGN;
            ledAssignWaiting();
            Serial.println("ASSIGN: WAITING");
        }

    } else if (cmd.length() > 0) {
        Serial.print("ERROR: Unknown command: ");
        Serial.println(cmd);
    }
    // Empty commands are silently ignored.
}

// ---------------------------------------------------------------------------
// Main loop
// ---------------------------------------------------------------------------

void loop() {
    // --- Handle incoming serial commands ---
    if (Serial.available() > 0) {
        String cmd = Serial.readStringUntil('\n');
        handleCommand(cmd);
    }

    // --- If reader is off, do nothing ---
    if (currentMode == MODE_OFF) {
        return;
    }

    // --- Check for a new card ---
    if (!mfrc522.PICC_IsNewCardPresent()) {
        return;
    }
    if (!mfrc522.PICC_ReadCardSerial()) {
        return;
    }

    // --- Debounce ---
    unsigned long now = millis();
    if (now - lastReadTime < DEBOUNCE_MS) {
        mfrc522.PICC_HaltA();
        return;
    }
    lastReadTime = now;

    // --- Extract UID ---
    String uid = readUID();

    // --- Send UID (same format regardless of mode) ---
    Serial.print("UID: ");
    Serial.println(uid);

    // --- Handle mode-specific post-scan behaviour ---
    if (currentMode == MODE_ASSIGN) {
        // One card scanned in ASSIGN mode: return to NORMAL.
        currentMode = MODE_NORMAL;
        ledCardFlash();
        Serial.println("ASSIGN: DONE");
    } else {
        // MODE_NORMAL: just flash the card LED.
        ledCardFlash();
    }

    // Halt the card so it is not read again on next loop iteration.
    mfrc522.PICC_HaltA();
    mfrc522.PCD_StopCrypto1();
}
