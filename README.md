# Power Supply

## Summary
- Custom linear-regulator power supply boards providing multiple regulated rails from a raw input voltage.
- **v0.1:** 24V input board with four fixed regulators (15V, 12V, 5V, 3V3) plus two variable outputs via jumpers and a buck converter; four-layer 157.5mm x 100mm PCB.
- **v1.1:** Revised ±15V input board delivering +12V and -5V, entirely from linear regulators (12V now derived from 15V instead of 24V, plus a new -5V rail); same 4-layer 157.5mm x 100mm PCB footprint.

## v0.1

### Specs
- **Input:** +24V
- **Output:** +15V, +12V, +5V, +3V3, two variable voltage outputs
- **Description:** one output for each voltage value and two variable outputs via jumpers. Uses four regulators and one buck converter.
- **PCB:** 157.5mm x 100mm / 4 Layer

### 15V Voltage Regulator
- **Input:** 24V, GNDA
- **Output:** 15V, GNDA
- **Maximum Output Current:** 1A
- **Thermal Resistance:** 44.8°C/W

### 12V Voltage Regulator
- **Input:** 24V, GNDA
- **Output:** 12V, GNDA
- **Maximum Output Current:** 1A
- **Thermal Resistance:** 44.8°C/W

### 5V Voltage Regulator
- **Input:** 24V, GNDA
- **Output:** 5V, GNDA
- **Maximum Output Current:** 1.2A
- **9V Buck Converter Thermal Resistance:** 50°C/W
- **5V Linear Regulator Thermal Resistance:** 44.8°C/W

### 3V3 Voltage Regulator
- **Input:** 9V, GNDA
- **Output:** 3V3, GNDA
- **Maximum Output Current:** 5A
- **Thermal Resistance:** 44.8°C/W

### Design Process
- Thermal vias for each regulator for heat dissipation
- Buck converter routed to minimize loop area of interference
- Built four voltage regulators separately, then tested out ideas on how to put together a combined power supply board
- Settled on two variable outputs and four constant outputs

## v1.1

### Specs
- **Input:** +15V, -15V, GNDA
- **Output:** +12V, -5V, GNDA
- **Description:** three output ports for each voltage value. All regulated from linear regulators
- **PCB:** 157.5mm x 100mm / 4 Layer

### 12V Voltage Regulator
- **Input:** 15V, GNDA
- **Output:** 12V, GNDA
- **Maximum Output Current:** 1.5A
- **Thermal Resistance:** 19°C/W

### -5V Voltage Regulator
- **Input:** -15V, GNDA
- **Output:** -5V, GNDA
- **Maximum Output Current:** 1A
- **Thermal Resistance:** 65°C/W

### Design Changes
- Needed -5V supply so made a new version of the board with 12V and -5V
- 12V now regulated from 15V instead of 24V, so used a different regulator
