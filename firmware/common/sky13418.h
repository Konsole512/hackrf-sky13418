/*
 * SKY13418-485LF SP8T switch support for HackRF (DF development).
 *
 * This file is part of HackRF.
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2, or (at your option)
 * any later version.
 */

#pragma once

#ifdef SKY13418_SWITCH

	#include "operacake.h"

/*
 * Wiring (P20 expansion header -> SKY13418 eval board):
 *
 *   P20 pin 9  GPIO3_12 / P7_4 / CTOUT_13  -> V3 (LSB)  via divider to ~1.8 V
 *   P20 pin 10 GPIO3_13 / P7_5 / CTOUT_12  -> V2        via divider to ~1.8 V
 *   P20 pin 5  GPIO3_8  / P7_0 / CTOUT_14  -> V1 (MSB)  via divider to ~1.8 V
 *   P20 pin 3  3V3AUX                      -> VDD
 *   P20 pin 13 GND                         -> GND
 *
 * SKY13418 control inputs are limited to +3.0 V absolute maximum, so the
 * 3.3 V SCT outputs must be divided or level shifted.
 *
 * Opera Cake port index maps 1:1 onto the SKY13418 truth table:
 *
 *   port  host name  V1 V2 V3  SKY port  use
 *   0     A1         0  0  0   RF1       North
 *   1     A2         0  0  1   RF2       East
 *   2     A3         0  1  0   RF3       South
 *   3     A4         0  1  1   RF4       West
 *   4     B1         1  0  0   RF5       50 ohm sync marker
 *   5     B2         1  0  1   RF6       spare / calibration
 *   6     B3         1  1  0   RF7       unused (terminate)
 *   7     B4         1  1  1   RF8       unused (terminate)
 */

	/* The switch is presented to the host as Opera Cake address 0. */
	#define SKY13418_BOARD_ADDRESS 0

	#define SKY13418_PORT_NORTH  OPERACAKE_PA1
	#define SKY13418_PORT_EAST   OPERACAKE_PA2
	#define SKY13418_PORT_SOUTH  OPERACAKE_PA3
	#define SKY13418_PORT_WEST   OPERACAKE_PA4
	#define SKY13418_PORT_MARKER OPERACAKE_PB1

	/* Port selected at boot and whenever no plan is configured. */
	#define SKY13418_PORT_DEFAULT SKY13418_PORT_MARKER

#endif /* SKY13418_SWITCH */
