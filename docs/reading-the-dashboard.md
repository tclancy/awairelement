# Reading the dashboard

What each card means, and what a gap, a shaded span or a badge on it is telling you. This text used to be the footer of the dashboard itself; it moved here so the page is charts and the explanation is somewhere it can be read.

## Every card

Times shown in your local timezone. Shaded spans are alert events (band shows each
bucket's low–high range; line is the average). Hovering reads one bucket: the `low` and
`high` in a chart's legend are that bucket's, not the range's — the range's highest
single reading is the **peak** beside the current value on each of the six indoor cards.
Data: 30-second polls from the Awair Element local API.

## Temp

**Temp** is one temperature per place: the Awair's own room, the outdoor temperature
from quarter-hourly Open-Meteo readings held between publishes, the basement sensor
averaged per bucket, and the bedroom mini split head's own reading read through the
climate bridge. They share one axis because they are all a temperature in the same unit,
and the distance between them is what the card is for. A break in a line is that
source's own gap — an outdoor-poller gap, a sensor or basement-poller gap, a bridge or
mini-split-poller gap — and never a weather event. The bedroom line is also left off
once that head has been switched off for more than 30 minutes, because the bridge keeps
reporting an off head's last reading.

## Humidity

**Humidity** is the same idea for the other quantity: the Awair's reading, the basement
sensor's, and each mini split head's own, all in percent on one axis. A head's line is
missing entirely if that head does not report humidity — whether these units do has
never been confirmed — so on this card a flat absence is "no such reading", not
necessarily an outage.

## Great room and Bedroom

The two **room** cards are a room beside the mini split head that serves it. **Great
room** draws the Awair's average as its room line — the same series the Temp card draws,
from a sensor in the open hall rather than in the room itself — with the dining head's
own temperature beside it. **Bedroom** draws that head's temperature, the setpoint it
was sent and the climate bridge's target. On both, **Draw** is the exception: it is the
head's metered power draw in watts, read against the **right-hand axis**, so its height
is not comparable with any temperature on the card. It is the energy the head reported
over its own window divided by that window — not its spot reading, which sat at a
constant 30.0 W while the metered energy said 45–65 W. A break in it is a window the
head reported no energy for, or the same bridge or mini-split-poller gap that breaks the
temperature beside it; a flat zero is a head that drew nothing, which is a reading
rather than a gap.

## Main zone humidity

**Main zone humidity** is the great room — the room the wood stove is in — against the
target the house is held to in winter: **40%**, dropping to **35%** when it is **10.4 °F
(−12 °C)** or colder outside, because indoor moisture condenses on the coldest window in
the room and a cold pane cannot carry as much. The dashed line is that target, and it
steps where the outdoor temperature crosses the threshold; with no outdoor reading it
holds the warmer target, so a dead outdoor poller cannot quietly relax it. Three sensors
are drawn because three sensors are in that one room and they disagree: the **Awair**
sits high above the fireplace, so it reads warmer than living level and therefore a
lower relative humidity for the same moisture; the **humidifier** reads at the floor;
the **dining** mini split head reads at head height. Which one counts as the zone is a
setting. Shading is the humidifier not humidifying: **grey** is switched off, **red** is
out of water or the tank lifted out — a sag under a red span is a refill, under a grey
one a setting. A humidifier reporting nothing at all is unshaded, because "we could not
read it" is not "it was off". The hours below target under the header are always the
**last seven days** whatever range is shown, and count only time we have readings for: a
poller outage contributes nothing, so the figure is a floor rather than an estimate.

## Dew point

**Dew point** is every place that records both a temperature and a humidity, on one
axis. It is the chart that makes a warm room comparable with a cool one: the stove
lowers the great room's relative humidity mostly by *heating the air*, with no water
having left the room, where dew point moves only when the moisture does. In one open
room the great room sensors' dew points should roughly agree — a standing gap between
them means the air there is not mixing as freely as the room looks. All of them are
computed here from one formula, including the Awair's, which publishes its own: six
places read against six formulas would not be a comparison.

## The basement watch

A badge under the Temp card's header is the **basement watch**: the basement went above
62 °F during the hours the room is unoccupied, and the outdoor high of the 24 hours
before that was below 62 °F too, so the weather does not account for it. It holds until
a reading comes back to 62 °F or below, so the temperature it shows is **now** and the
outdoor high is **then** — they can be up to 42 hours apart, which is why the badge says
which is which. It states a fact and asks for nothing. No badge covers three different
things — the basement is below 62 °F, the hours do not apply, or no reading can be
judged — and an outdoor-poller gap is the third of those, never a badge.

The main zone targets and the outdoor temperature where the target steps are settings: `AWAIR_HUMIDITY_TARGET`, `AWAIR_HUMIDITY_TARGET_COLD` and `AWAIR_HUMIDITY_COLD_OUTDOOR_C`. The numbers above are the defaults.
