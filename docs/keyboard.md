# Keyboard shortcuts and the command palette

Every page has the same keyboard: a few single-key shortcuts and a **command palette** that
finds everything else by name. None of it works while you are typing or choosing in a field
(the config editor, a number box, a dropdown, a slider), so a key never lands in the wrong
place. The code is `tf_daemon/static/keys.js` (shared) and each page's own commands
(`static/audio/commands.js`, the end of `rf.js` and `qlxd.js`).

On a Mac, **Ctrl** below is **⌘**.

## Help: every command

Press **?** on any page (or choose *Keyboard shortcuts and commands* in the palette) for every
command the page has, by group, with its shortcut. Click one to run it; **?** or **Esc**
closes it.

## The command palette

Press **/** (or **Ctrl+K**) on any page.

| Key | In the palette |
|---|---|
| type | filter, best match first: the start of a word (`eq un` finds *EQ · Undo*, `12` or `sn` finds *12 · Snare*), and lazy typing too: typos (`snaer`, `wterfall`) and letters left out (`rstrt srv` finds *Restart server…*) |
| **Tab** / **Shift+Tab** | next / previous command (also **↓** / **↑**, **Ctrl+N** / **Ctrl+P**) |
| **Enter** | run the highlighted command (or click one) |
| **Esc** | close (or click outside it) |

A command shows up only when it can be used: the EQ commands only while the desk EQ is shown
and editable, *Paste* only after a *Copy*, *Mic check* only when the vocal assist is on.
Commands with a shortcut show it on the right.

## Every page

| Shortcut | Command | Does |
|---|---|---|
| **Shift+→** / **Shift+←** | Next page / Previous page | the page tabs, in order, wrapping round |
| | Go to Audio / RF / Wireless / Config | |
| **?** | Keyboard shortcuts and commands | this list, on the page |
| **F** | Full screen / Leave full screen | the browser's full screen (a tablet at the desk) |
| | Reload page | |
| | Check for updates | opens the Config page, which checks |
| | Update now… | opens the Config page and asks before pulling the update and restarting |
| | Run another branch or commit… | opens the Config page at the branch / commit field |
| | Restart server… | asks first; every page's live view drops for a few seconds |

## Audio page

| Shortcut | Command | Does |
|---|---|---|
| **[** / **]** | Previous channel / Next channel | hold to keep stepping |
| **1** … **8** | Tile *n* of this bank | the bank shown in the channel bar |
| | *number* · *name* | any input: type its number or name |
| **E** | Show / hide the desk EQ | on the spectrum (when the TF5's EQ is read) |
| **Ctrl+Z** | EQ · Undo | while the EQ is shown and editable |
| **Ctrl+Shift+Z**, **Ctrl+Y** | EQ · Redo | |
| | EQ · EQ on / off, HPF on / off | as the toolbar's EQ and HPF buttons |
| | EQ · Compare (A/B) | with the copied EQ |
| | EQ · Gain flat, Copy, Paste | as the toolbar's ⋯ menu (Paste asks first) |
| | EQ · Compare with before | the EQ as it was before your changes here |
| | Layout: Spectrum / Both / Waterfall | |
| | Real-time / Average trace on / off, Peak hold on / off, Reset peak hold | |
| | Ring markers on / off, Ring list on / off, Clear past rings | |
| | Console sidebar on / off, Suggested EQ on / off | |
| | Mic check | opens the vocal assist's panel |

## RF page

| Shortcut | Command | Does |
|---|---|---|
| **S** | Start sweep / Stop sweep | |
| | Connect / Disconnect | the tinySA |
| | Reset peak hold, Fit to receivers | |
| | Peak hold / Waterfall / Receiver lines on / off | |

## Wireless page

| Command | Does |
|---|---|
| Network settings | opens the network panel |
| Rescan for receivers | |
| Sort by … | each of the Sort choices |

## Adding a command

A page registers its commands with `addCommands()` from `static/keys.js`: a list, or a
function returning one (called each time the palette opens, so titles and `when` can follow
the page). Each command is `{title, run, group?, keys?, when?}`; `keys` are like `"e"`, `"["`,
`"mod+z"`, `"mod+shift+z"` (`mod` is Ctrl or ⌘). `help: false` keeps a command out of the
**?** list (one per input would bury the rest); a command without `run` is only a line there.
Make `run` press the control the command stands for, so a key does exactly what a tap does,
and add it to this page.
