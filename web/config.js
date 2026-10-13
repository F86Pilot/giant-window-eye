// =====================================================================
//  GIANT EYE SETTINGS
//  These are the STARTING values. The easiest way to change them is the
//  Settings tab on the phone page (http://<tracker-ip>:8085/calibrate): changes
//  there show up instantly and are saved, overriding this file.
//  (Editing this file still works; open eye pages reload when it's saved.)
// =====================================================================
window.EYE_CONFIG = {

  // ---- Look of the eye ----------------------------------------------
  // Iris color: 0=red, 22=orange, 45=gold, 120=green, 200=blue, 280=purple, 360=red again
  hueDrift: true,          // slowly change color over time (false = always irisHue)
  hueFrom: 0,              // drift between this color...
  hueTo: 360,              // ...and this one, then back. For a full rainbow: 0 and 360
  hueCycleMinutes: 10,     // minutes for one full trip there and back
  irisHue: 22,             // color used when hueDrift is false
  irisSize: 0.42,          // iris size compared to the whole eyeball (0.3 - 0.55)
  pupilSize: 0.40,         // pupil size compared to the iris (0.25 - 0.6)
  scleraBrightness: 0.75,  // brightness of the "white" of the eye (0 = black, 1 = full white)
  veins: true,             // red veins on the white of the eye

  // ---- Fitting the eye to the round window ---------------------------
  eyeScale: 0.96,          // eyeball size compared to the screen height (1 = touches top and bottom)
  eyeShiftX: 0,            // move the whole eye left/right on screen (-0.2 to 0.2)
  eyeShiftY: 0,            // move the whole eye up/down on screen (-0.2 to 0.2)

  // ---- Movement --------------------------------------------------------
  maxTravel: 0.45,         // how far the iris can move from center (fraction of eyeball)
  smoothing: 5,            // how quickly the eye follows a person (higher = snappier)
  idleAfterSec: 2.5,       // seconds with nobody seen before the eye starts glancing around
  idleUpMost: 0.4,         // when nobody's around, how far UP the eye may glance (0 = level)
  idleDownMost: 0.05,      // ...and how far down (keep small so looking down at people is dramatic)
  blinkMinSec: 2.5,        // shortest time between blinks
  blinkMaxSec: 7,          // longest time between blinks

  // ---- Moods ------------------------------------------------------------------
  moods: true,             // false = no eye rolls or double-takes
  drowsySec: 6,            // Sleep test button: how long it fights to stay awake before its eyes close
  eyeRollChance: 0.04,     // chance of an eye roll each time it glances around while idle (0 to 1)
  doubleTakeChance: 0.4,   // chance of a double-take when someone new shows up (0 to 1)

  // ---- Aiming --------------------------------------------------------------
  // Where the eye looks for a given person is set on the calibration page:
  // open http://<tracker-ip>:8085/calibrate on your phone.

  // ---- Detection (used by tracker.py) -------------------------------------
  detectConfidence: 0.35,  // how sure the camera must be before following someone (lower = more sensitive)

  // ---- Connection --------------------------------------------------------
  wsUrl: "",               // leave blank: connects back to the server the page came from

  // ---- Performance --------------------------------------------------------
  maxPixelRatio: 2,        // 1 is fine for the Pi; 2 looks sharper on laptops/phones
};
