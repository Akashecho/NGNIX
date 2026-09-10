#include <ESP32Servo.h>

#define SERVO1_PIN 18
#define SERVO2_PIN 19

Servo servo1;
Servo servo2;

void sweepOne(Servo &s, const char *label) {
  Serial.print(label);
  Serial.println(": sweeping 0 -> 180");
  for (int angle = 0; angle <= 180; angle += 2) {
    s.write(angle);
    delay(15);
  }
  delay(400);
  Serial.print(label);
  Serial.println(": sweeping 180 -> 0");
  for (int angle = 180; angle >= 0; angle -= 2) {
    s.write(angle);
    delay(15);
  }
  delay(400);
  s.write(90);
  Serial.print(label);
  Serial.println(": back to 90 (center)");
  delay(600);
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("TARS servo test starting");

  servo1.setPeriodHertz(50);
  servo2.setPeriodHertz(50);
  bool ok1 = servo1.attach(SERVO1_PIN, 500, 2400);
  bool ok2 = servo2.attach(SERVO2_PIN, 500, 2400);

  Serial.print("servo1.attach() returned: ");
  Serial.println(ok1 ? "OK" : "FAILED");
  Serial.print("servo2.attach() returned: ");
  Serial.println(ok2 ? "OK" : "FAILED");
  Serial.print("servo1.attached(): ");
  Serial.println(servo1.attached() ? "true" : "false");
  Serial.print("servo2.attached(): ");
  Serial.println(servo2.attached() ? "true" : "false");

  servo1.write(90);
  servo2.write(90);
  Serial.println("Both servos centered at 90. Starting individual test in 2s...");
  delay(2000);
}

void loop() {
  Serial.println("--- Testing SERVO 1 (GPIO18) only. Servo 2 stays still. ---");
  sweepOne(servo1, "SERVO1");

  delay(1000);

  Serial.println("--- Testing SERVO 2 (GPIO19) only. Servo 1 stays still. ---");
  sweepOne(servo2, "SERVO2");

  delay(1500);
  Serial.println("=== Cycle complete. Repeating in 3s. ===");
  delay(3000);
}
