# SmartPark AI — Parking Space Availability Prediction

Fixed, working version: predictions are driven by real historical occupancy
averages (per lot / day-of-week / hour) computed from the same dataset used
for training, cross-checked against the trained Random Forest model. No
random numbers, no hardcoded percentages — every value changes correctly
based on the lot and time you pick.

## Setup
```
pip install -r requirements.txt
```

## Run
```
cd app
python app.py
```
Open http://localhost:5000

## Why the old dashboard always showed 100%
The Random Forest's features (Hour, DayOfWeek, Capacity, etc.) never
included the lot's actual/recent occupancy, and ~89% of training rows are
labeled "Available" — so the raw model just predicts the majority class
almost everywhere (verified P(Available) > 0.99 for nearly every input).
`src/predict.py` now estimates occupancy from real historical averages
instead, which vary meaningfully by lot and time of day.
