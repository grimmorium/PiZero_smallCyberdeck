#include <Wire.h>

// Definicje pinów
const int PIN_X = A0;
const int PIN_Y = A1;
const int PIN_SW = 2;

// Adres I2C dla Arduino (można zmienić, np. 0x08)
const int I2C_ADDRESS = 0x28;

// Zmienne globalne do przechowywania aktualnego stanu
volatile int posX = 0;
volatile int posY = 0;
volatile byte buttonState = 1; // 1 = nie wciśnięty (pull-up), 0 = wciśnięty

void setup() {
  // Konfiguracja przycisku z wbudowanym rezystorem podciągającym
  pinMode(PIN_SW, INPUT_PULLUP);
  
  // Dołączenie do magistrali I2C z określonym adresem
  Wire.begin(I2C_ADDRESS);
  
  // Rejestracja funkcji wywoływanej przy żądaniu danych od Mastera (RPi)
  Wire.onRequest(requestEvent);
  
  Serial.begin(9600);
}

void loop() {
  // Ciągły odczyt wartości z joysticka
  posX = analogRead(PIN_X);
  posY = analogRead(PIN_Y);
  buttonState = digitalRead(PIN_SW);
  
  // Krótkie opóźnienie dla stabilności odczytów ADC
  delay(10);
}

// Funkcja wykonywana za każdym razem, gdy RPi zażąda danych
void requestEvent() {
  // Przygotowanie 5-bajtowego bufora na dane:
  // [0] X starszy bajt (High)
  // [1] X młodszy bajt (Low)
  // [2] Y starszy bajt (High)
  // [3] Y młodszy bajt (Low)
  // [4] Stan przycisku
  
  byte buffer[5];
  
  buffer[0] = (posX >> 8) & 0xFF;
  buffer[1] = posX & 0xFF;
  
  buffer[2] = (posY >> 8) & 0xFF;
  buffer[3] = posY & 0xFF;
  
  buffer[4] = buttonState;
  
  // Wysłanie bufora przez I2C
  Wire.write(buffer, 5);
}
