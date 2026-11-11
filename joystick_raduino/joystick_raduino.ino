#include <Wire.h>

// Pin definitions
const int PIN_X = A0;
const int PIN_Y = A1;
const int PIN_SW = 2;

// I2C address of the Arduino (can be changed, e.g. to 0x08)
const int I2C_ADDRESS = 0x28;

// Global variables holding the current state
volatile int posX = 0;
volatile int posY = 0;
volatile byte buttonState = 1; // 1 = released (pull-up), 0 = pressed

void setup() {
  // Configure the button with the built-in pull-up resistor
  pinMode(PIN_SW, INPUT_PULLUP);

  // Join the I2C bus at the given address
  Wire.begin(I2C_ADDRESS);

  // Register the function called when the master (RPi) requests data
  Wire.onRequest(requestEvent);

  Serial.begin(9600);
}

void loop() {
  // Continuously read the joystick values
  posX = analogRead(PIN_X);
  posY = analogRead(PIN_Y);
  buttonState = digitalRead(PIN_SW);

  // Short delay for stable ADC readings
  delay(10);
}

// Function executed every time the RPi requests data
void requestEvent() {
  // Prepare a 5-byte buffer with the data:
  // [0] X high byte (High)
  // [1] X low byte (Low)
  // [2] Y high byte (High)
  // [3] Y low byte (Low)
  // [4] Button state

  byte buffer[5];

  buffer[0] = (posX >> 8) & 0xFF;
  buffer[1] = posX & 0xFF;

  buffer[2] = (posY >> 8) & 0xFF;
  buffer[3] = posY & 0xFF;

  buffer[4] = buttonState;

  // Send the buffer over I2C
  Wire.write(buffer, 5);
}
