# Ideas / todo

Things we plan to build at some point, a line or two each on what and why. Delete an idea
once it ships or we drop it.

- **Connect the new channel-strip features to the real TF5 and test them there.** The
  input (head-amp gain, +48V, phase), gate, compressor and AUX-send controls work in demo
  mode but are only simulated by the app, or edited on the simulated desk, so they do
  nothing on the board yet. For each one: record its parameter names from a real desk,
  add the read and the guarded write, then check it on the TF5 (the ranges in
  `console/strip.py` SPECS still need confirming too). What's connected and what's
  missing is tracked in [desk-coverage.md](desk-coverage.md).
- **Audio page: every channel and fader the TF StageMix app has, not just the USB ones.**
  The page only lists the inputs that come in over USB audio. Show the rest too, with
  their fader, EQ and strip controls, so the page covers the whole desk like StageMix.
  Channels with no USB audio just don't get the spectrum or ring detection.
- **Multitrack recording tab.** A new page to record shows: each USB input channel to its
  own track, with start / stop, levels while recording, and the takes listed for download.
- **Home page of big mid-show alerts.** One glanceable page for during a performance:
  large warnings for low pack batteries, feedback / ringing (with the channel it's on), and
  anything else worth catching mid-show (e.g. RF dropouts or interference, a receiver or
  the desk going offline).
- **RF dropout detection and logging (rehearsals and shows).** Log every mic dropout with
  the time, channel and RF state, and tell the two kinds apart: a cutout while the actor
  is on stage and talking (audible to the audience, likely RF interference, worth fixing)
  versus the pack just going out of range while the actor is off stage (harmless). Signals
  to combine: the QLX-D's RF / antenna levels, whether the channel had voice just before,
  and the fader being up. Review the log after rehearsal to find bad spots or frequencies.
