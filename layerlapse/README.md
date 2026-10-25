# Radicals LayerLapse

Automatic 3D-print timelapse on your phone. Draw a box where the toolhead parks;
every time the head parks there, the phone takes a photo. When the print is done,
the app turns the photos into a video you can save to your gallery.

Built for the Elegoo Centauri Carbon (CC1), but it works with any printer, because
it only uses the camera.

## 1. Put it online (once, free, with GitHub Pages)

The camera only works on an HTTPS page, so the app needs to be hosted.

1. Make a free account at github.com.
2. Click **New repository**, name it `layerlapse`, set it to **Public**, and create it.
3. Click **uploading an existing file**, drag in every file from this folder
   (including the `desktop` folder), and click **Commit changes**.
4. Go to **Settings → Pages**. Under *Branch*, pick `main` and `/ (root)`, then **Save**.
5. After about a minute the app is at `https://YOUR-USERNAME.github.io/layerlapse/`.

## 2. Install on the phone

Open that link in Chrome on the phone, allow the camera, then use the menu (⋮)
→ **Add to Home screen** / **Install app**. It opens full screen like a normal app.

## 3. Slicer (once)

Replace the contents of Elegoo Slicer → Printer settings → Machine G-code →
**Layer change G-code** with `park_gcode.txt`, then re-slice your models. Each
layer, the head retracts a little, lifts, parks in the back-right corner and waits 1.2 seconds.
To hide the small mark where each layer restarts, paint the seam onto a hidden face.

## 4. Marker (once)

Stick a bright marker on the toolhead (pink works best; avoid orange or red, the
belts are that colour). Keep it thin and away from the right side of the head:
on the CC1 the head presses the filament cutter there during colour changes, and
anything sticking out on that side will hit the case.

## 5. Set up each view (Front / Top)

1. Mount the phone, plug it in, open the app and pick **Front** or **Top**.
2. **Draw box**: drag over the spot where the toolhead parks.
3. **Marker**: the picture pauses; tap the marker on the toolhead. It can be
   anywhere on screen, even while the head is moving.
4. When the head parks, the box turns green with white dots on the marker.

Redo the box whenever you move the phone. Without a marker you can use
**Background** instead (tap it while the box is empty), but anything that passes
through the box, like the X-axis rail, can then trigger a photo.

## 6. Each print

1. Tap **Start timelapse**, then start the print.
2. Leave the app open. After a minute the screen goes dark to save power; tap to
   wake. Don't lock the phone or switch apps while it records.
3. Tap **Stop** at the end (or let auto-stop do it). Pick the **Video length**, then
   **Make video** → **Save / Share**. You can change the length and make it again.

While recording, the app shows how many parks it missed and why, e.g.
“missed 1 (too soon after the last shot)”.

## Focus, zoom, brightness

Tap the target icon at the top. What shows depends on what your phone's browser
allows: a **Focus** slider (Auto, or manual from near to far), **Zoom** and
**Brightness**. Settings are remembered. If there's no slider, tap the screen to
focus on that spot, or try another lens under Settings → Camera.

## Settings

| Setting | Default | What it does |
|---|---|---|
| Theme | System | System, Light or Dark. The camera view stays dark. |
| Marker: minimum share of the box | auto | How much of the box must be marker-coloured. Set when you pick the marker. |
| Marker: colour tolerance | ±18° | How far the colour may drift. Raise it in dim light. |
| Background: sensitivity | 18 | Only used without a marker. Lower = more sensitive. |
| Hold still before shooting | 0.4 s | How long the parked head must be still. Keep it well under the 1.2 s pause. |
| Minimum time between shots | 1.5 s | Stops one park from being shot twice. Leave it at 1.5 s; higher values skip fast layers. |
| Auto-stop | 10 min | Stops recording when no new photo comes for this long. 0 = off. |
| Video length | 15 s | Total length of the finished video, including 2 s on the finished print. The speed follows from the number of shots. Can also be changed on the results screen. |

## Your logo

Upload a file named `logo.png` to the repository (a light-coloured logo on a
transparent background works best, about 240 × 52 px). The app shows it in place
of the RADICALS wordmark.

## Updating the app

Upload the changed files to the same repository; files with the same name are
replaced. Close and reopen the app on the phone while online to get the new version.

## Desktop version

The `desktop` folder has the earlier PC version (Python), which talks to the
printer over Wi‑Fi and uses the phone through the IP Webcam app.
