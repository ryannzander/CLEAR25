# How CLEAR Uses Wind — Explained Simply

*(No technical background needed.)*

## The idea in one sentence

Smoke doesn't teleport — it rides the wind. So if you know **where the smoke is
sitting right now** and **which way the wind is blowing**, you can predict
whether it's headed for Toronto **before it arrives**.

## The picture to keep in your head

Imagine standing in downtown Toronto and dividing everything around you into
8 pie slices: North, Northeast, East, Southeast, South, Southwest, West,
Northwest. In every slice there are air-quality stations reporting how smoky
their air is, every hour.

Now add one more instrument: a weather vane and a wind gauge.

- If the **Northwest slice** is full of smoke and the wind is blowing **from
  the northwest**, that smoke is on a conveyor belt pointed at the city.
- If the same slice is smoky but the wind blows the **other way**, it's
  someone else's problem.

Without wind, our computer model had to treat every slice as equally
suspicious. With wind, it knows **which slice actually matters right now**.

## The five wind "clues" the model gets every hour

Every hour, the system turns the raw wind reading into five simple clues:

1. **How hard is it blowing?** (calm air moves smoke slowly; strong wind moves
   it fast)
2. **Which direction — part 1** and
3. **Which direction — part 2** (the direction is stored as two numbers so the
   computer understands that "359°" and "1°" are basically the same direction,
   not opposite ends of a scale)
4. **How much smoke is sitting in the upwind slice?** — the star clue. The
   model looks at the slice the wind is coming *from* and asks "how bad is the
   air over there?"
5. **Smoke × speed** — a "delivery rate": lots of upwind smoke arriving on a
   fast wind is worse than the same smoke on a light breeze.

One safety rule: if the wind reading is ever *missing* for an hour, the system
marks it "unknown" rather than pretending it was calm — a data outage is never
mistaken for clean air.

## Where the wind data comes from

**For teaching the model (the past):** we downloaded 22 years of hour-by-hour
historical wind for Toronto (2003–2025) from a free weather-history archive,
and lined it up hour-for-hour with 22 years of air-quality measurements.

**For running it live (the present):** every 3 hours, an automated job reads
the current wind — and the forecast wind — from Environment Canada's official
weather model. We checked carefully that the live wind "speaks the same
language" as the historical wind the model learned from (same units, same
direction convention), so what the model learned in school is exactly what it
sees on the job.

## How the model learned

We showed the computer 22 years of history, hour by hour: *"here's the smoke
map around Toronto, here's the wind — did Toronto's air turn bad 6, 12, 24, or
48 hours later?"* Over roughly 190,000 example hours (including every major
smoke event since 2003), it learned the patterns that come before bad air.

Then we tested it on years it had **never seen**. Two results stood out:

- With wind, the model finally **beat the naive guess** ("tomorrow will be
  like right now") at every time range.
- Wind helped **most for the long-range predictions** (1–2 days out) and
  barely at all for the next few hours — which is exactly what physics says
  should happen: the further ahead you look, the more it matters *where the
  smoke is coming from* rather than *what's already here*.

That's a good sign the model learned something real, not a fluke.

## What happens every hour, live

1. A timer fires once an hour.
2. The system grabs the latest readings from the ~200 stations around Toronto
   and the latest Environment Canada wind.
3. It computes the five wind clues and adds them to a rolling 24-hour diary of
   what the air and wind have been doing.
4. The trained model reads that diary and answers four questions: *what's the
   chance Toronto's air turns smoky in 6 hours? 12? 24? 48?*
5. Those four percentages appear on the dashboard as the "Toronto Smoke
   Forecast" bars.

## The important fine print

The forecast is a **heads-up, not an alarm**. CLEAR's actual alerts still come
only from the original three-rule system that was validated for the research —
the wind-aware forecast is displayed beside it but is not allowed to trigger
or change an alert. We tested whether it *should* be allowed to (replaying it
against five years of history), and found that while it sees smoke coming much
earlier, it also raises too many false warnings to meet the project's
standard — so it stays informational until it can clear that bar.
