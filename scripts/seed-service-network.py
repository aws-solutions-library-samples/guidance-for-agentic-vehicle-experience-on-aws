"""Seed adp-dev-service-network with expanded service-center coverage.

Aligns the service-network table with the fleet simulator's 6 city
roster: Seattle, Atlanta, Dallas, Chicago, Denver, Munich. Each city
gets a varied mix of types (dealer, independent, quick-service,
fleet-service, body-shop, tire-specialist) so find_service_center
returns useful, distance-sorted results regardless of which simulated
vehicle is asking.

Idempotent: uses PutItem on a known centerId, so re-running overwrites
in place rather than duplicating. Existing pre-seeded centers in other
cities (Phoenix, LA, Miami, etc.) are untouched.

Usage:
    python3 scripts/seed-service-network.py
"""

import boto3
from decimal import Decimal

_TABLE_NAME = "adp-dev-service-network"
_REGION = "us-east-1"

# Standard hours blocks reused across multiple centers.
HOURS_DEALER = {"weekday": "7:00 AM - 7:00 PM", "saturday": "8:00 AM - 5:00 PM", "sunday": "Closed"}
HOURS_QUICK = {"weekday": "7:00 AM - 8:00 PM", "saturday": "7:00 AM - 6:00 PM", "sunday": "9:00 AM - 4:00 PM"}
HOURS_FLEET = {"weekday": "6:00 AM - 10:00 PM", "saturday": "7:00 AM - 5:00 PM", "sunday": "Closed"}
HOURS_INDEP = {"weekday": "7:30 AM - 7:00 PM", "saturday": "8:00 AM - 5:00 PM", "sunday": "Closed"}
HOURS_BODY = {"weekday": "7:30 AM - 6:00 PM", "saturday": "8:00 AM - 12:00 PM", "sunday": "Closed"}
HOURS_TIRE = {"weekday": "8:00 AM - 7:00 PM", "saturday": "8:00 AM - 5:00 PM", "sunday": "Closed"}
HOURS_GERMAN = {"weekday": "7:00 - 18:00", "saturday": "8:00 - 13:00", "sunday": "Geschlossen"}

ALL_BRANDS = ["Chevrolet", "Ford", "Toyota", "Honda", "RAM", "GMC", "Nissan",
              "Hyundai", "Volkswagen", "Subaru"]
LUXURY_BRANDS = ["BMW", "Mercedes-Benz", "Audi", "Lexus", "Porsche", "Volvo"]
ALL_CAPS_FULL = ["oil-change", "brakes", "tires", "transmission", "engine",
                 "electrical", "hvac", "suspension", "diagnostics", "warranty-repair"]
QUICK_CAPS = ["oil-change", "tires", "filters", "wipers", "battery"]
TIRE_CAPS = ["tires", "alignment", "suspension"]
BODY_CAPS = ["body-work", "paint", "glass", "dent-repair", "frame-straightening"]
FLEET_CAPS = ALL_CAPS_FULL + ["fleet-maintenance-program"]
DEALER_CAPS = ALL_CAPS_FULL + ["recall-service"]


# fmt: off
# Each entry is the minimum set of fields that match the existing schema
# on adp-dev-service-network. Coords are within ~25mi of city centers,
# spread out so distance sorting produces meaningfully different results.
# Phone numbers and addresses are illustrative — real demo data would
# live in tenant config rather than here.
NEW_CENTERS = [
    # ------------------------------ Seattle (WA) ----------------------------
    dict(centerId="SC-SEA-101", name="Discount Tire — Seattle", type="tire-specialist",
         address="2601 4th Ave S", city="Seattle", state="WA", zipCode="98134",
         country="USA", phone="(206) 555-1010", latitude=47.5851, longitude=-122.3290,
         capabilities=TIRE_CAPS, brandsServiced=ALL_BRANDS, hours=HOURS_TIRE,
         rating=4.5, reviewCount=412, averageWaitDays=0,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=False,
         certifications=["TIA Certified Installer"]),
    dict(centerId="SC-SEA-102", name="Bellevue Honda", type="dealer",
         address="13291 SE 36th St", city="Bellevue", state="WA", zipCode="98006",
         country="USA", phone="(425) 555-2020", latitude=47.5779, longitude=-122.1685,
         capabilities=DEALER_CAPS, brandsServiced=["Honda"], hours=HOURS_DEALER,
         rating=4.6, reviewCount=798, averageWaitDays=2,
         mobileServiceAvailable=False, fleetDiscount=False, loanerVehicles=True,
         certifications=["Honda Certified", "ASE Certified"]),
    dict(centerId="SC-SEA-103", name="Caliber Collision — Seattle", type="body-shop",
         address="525 Mercer St", city="Seattle", state="WA", zipCode="98109",
         country="USA", phone="(206) 555-3030", latitude=47.6235, longitude=-122.3429,
         capabilities=BODY_CAPS, brandsServiced=ALL_BRANDS + LUXURY_BRANDS,
         hours=HOURS_BODY, rating=4.4, reviewCount=287, averageWaitDays=4,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=True,
         certifications=["I-CAR Gold Class"]),
    dict(centerId="SC-SEA-104", name="Penske Truck Leasing — Kent", type="fleet-service",
         address="20611 84th Ave S", city="Kent", state="WA", zipCode="98032",
         country="USA", phone="(253) 555-4040", latitude=47.4032, longitude=-122.2421,
         capabilities=FLEET_CAPS, brandsServiced=["Ford", "RAM", "GMC", "Chevrolet"],
         hours=HOURS_FLEET, rating=4.3, reviewCount=156, averageWaitDays=1,
         mobileServiceAvailable=True, fleetDiscount=True, loanerVehicles=False,
         certifications=["DOT Inspection Authorized", "Fleet Maintenance Certified"]),

    # ------------------------------ Atlanta (GA) ----------------------------
    dict(centerId="SC-ATL-101", name="Hennessy Mercedes-Benz — Buckhead", type="dealer",
         address="3399 Roswell Rd NE", city="Atlanta", state="GA", zipCode="30305",
         country="USA", phone="(404) 555-1111", latitude=33.8488, longitude=-84.3733,
         capabilities=DEALER_CAPS, brandsServiced=["Mercedes-Benz"], hours=HOURS_DEALER,
         rating=4.7, reviewCount=952, averageWaitDays=3,
         mobileServiceAvailable=False, fleetDiscount=False, loanerVehicles=True,
         certifications=["Mercedes-Benz Certified", "ASE Certified"]),
    dict(centerId="SC-ATL-102", name="Pep Boys — Atlanta", type="independent",
         address="2235 Cheshire Bridge Rd NE", city="Atlanta", state="GA", zipCode="30324",
         country="USA", phone="(404) 555-2222", latitude=33.8083, longitude=-84.3506,
         capabilities=ALL_CAPS_FULL, brandsServiced=ALL_BRANDS, hours=HOURS_INDEP,
         rating=4.0, reviewCount=634, averageWaitDays=1,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=False,
         certifications=["ASE Certified"]),
    dict(centerId="SC-ATL-103", name="Caliber Collision — Atlanta Midtown", type="body-shop",
         address="690 14th St NW", city="Atlanta", state="GA", zipCode="30318",
         country="USA", phone="(404) 555-3333", latitude=33.7868, longitude=-84.3998,
         capabilities=BODY_CAPS, brandsServiced=ALL_BRANDS + LUXURY_BRANDS,
         hours=HOURS_BODY, rating=4.5, reviewCount=312, averageWaitDays=5,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=True,
         certifications=["I-CAR Gold Class"]),
    dict(centerId="SC-ATL-104", name="Discount Tire — Atlanta", type="tire-specialist",
         address="3115 Piedmont Rd NE", city="Atlanta", state="GA", zipCode="30305",
         country="USA", phone="(404) 555-4444", latitude=33.8401, longitude=-84.3680,
         capabilities=TIRE_CAPS, brandsServiced=ALL_BRANDS + LUXURY_BRANDS,
         hours=HOURS_TIRE, rating=4.6, reviewCount=521, averageWaitDays=0,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=False,
         certifications=["TIA Certified Installer"]),
    dict(centerId="SC-ATL-105", name="AutoNation Toyota — Marietta", type="dealer",
         address="2060 Cobb Pkwy SE", city="Marietta", state="GA", zipCode="30060",
         country="USA", phone="(770) 555-5555", latitude=33.9219, longitude=-84.5180,
         capabilities=DEALER_CAPS, brandsServiced=["Toyota"], hours=HOURS_DEALER,
         rating=4.3, reviewCount=1102, averageWaitDays=2,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=True,
         certifications=["Toyota Certified", "ASE Certified"]),
    dict(centerId="SC-ATL-106", name="Jiffy Lube — Atlanta", type="quick-service",
         address="1801 Howell Mill Rd NW", city="Atlanta", state="GA", zipCode="30318",
         country="USA", phone="(404) 555-6666", latitude=33.7990, longitude=-84.4129,
         capabilities=QUICK_CAPS, brandsServiced=ALL_BRANDS, hours=HOURS_QUICK,
         rating=4.1, reviewCount=423, averageWaitDays=0,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=False,
         certifications=["ASE Certified"]),

    # ------------------------------ Dallas (TX) -----------------------------
    dict(centerId="SC-DAL-101", name="Caliber Collision — Dallas Uptown", type="body-shop",
         address="2724 N Stemmons Fwy", city="Dallas", state="TX", zipCode="75207",
         country="USA", phone="(214) 555-1212", latitude=32.8113, longitude=-96.8270,
         capabilities=BODY_CAPS, brandsServiced=ALL_BRANDS + LUXURY_BRANDS,
         hours=HOURS_BODY, rating=4.5, reviewCount=276, averageWaitDays=4,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=True,
         certifications=["I-CAR Gold Class"]),
    dict(centerId="SC-DAL-102", name="Pep Boys — Dallas", type="independent",
         address="2802 W Northwest Hwy", city="Dallas", state="TX", zipCode="75220",
         country="USA", phone="(214) 555-1313", latitude=32.8755, longitude=-96.8530,
         capabilities=ALL_CAPS_FULL, brandsServiced=ALL_BRANDS, hours=HOURS_INDEP,
         rating=4.0, reviewCount=599, averageWaitDays=1,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=False,
         certifications=["ASE Certified"]),
    dict(centerId="SC-DAL-103", name="Discount Tire — Dallas", type="tire-specialist",
         address="2725 Lemmon Ave E", city="Dallas", state="TX", zipCode="75204",
         country="USA", phone="(214) 555-1414", latitude=32.8121, longitude=-96.8086,
         capabilities=TIRE_CAPS, brandsServiced=ALL_BRANDS + LUXURY_BRANDS,
         hours=HOURS_TIRE, rating=4.7, reviewCount=687, averageWaitDays=0,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=False,
         certifications=["TIA Certified Installer"]),
    dict(centerId="SC-DAL-104", name="Jiffy Lube — Dallas", type="quick-service",
         address="6207 N Central Expy", city="Dallas", state="TX", zipCode="75206",
         country="USA", phone="(214) 555-1515", latitude=32.8542, longitude=-96.7770,
         capabilities=QUICK_CAPS, brandsServiced=ALL_BRANDS, hours=HOURS_QUICK,
         rating=4.0, reviewCount=311, averageWaitDays=0,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=False,
         certifications=["ASE Certified"]),

    # ------------------------------ Chicago (IL) ----------------------------
    dict(centerId="SC-CHI-101", name="Grossinger Auto Group — Chicago", type="dealer",
         address="6451 W Grand Ave", city="Chicago", state="IL", zipCode="60707",
         country="USA", phone="(773) 555-2121", latitude=41.9217, longitude=-87.7937,
         capabilities=DEALER_CAPS, brandsServiced=["Chevrolet", "GMC"], hours=HOURS_DEALER,
         rating=4.4, reviewCount=812, averageWaitDays=2,
         mobileServiceAvailable=False, fleetDiscount=False, loanerVehicles=True,
         certifications=["GM Certified", "ASE Certified"]),
    dict(centerId="SC-CHI-102", name="Pep Boys — Chicago", type="independent",
         address="3300 N Western Ave", city="Chicago", state="IL", zipCode="60618",
         country="USA", phone="(773) 555-2222", latitude=41.9415, longitude=-87.6886,
         capabilities=ALL_CAPS_FULL, brandsServiced=ALL_BRANDS, hours=HOURS_INDEP,
         rating=3.9, reviewCount=574, averageWaitDays=1,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=False,
         certifications=["ASE Certified"]),
    dict(centerId="SC-CHI-103", name="Caliber Collision — Chicago", type="body-shop",
         address="3415 W North Ave", city="Chicago", state="IL", zipCode="60647",
         country="USA", phone="(773) 555-2323", latitude=41.9097, longitude=-87.7090,
         capabilities=BODY_CAPS, brandsServiced=ALL_BRANDS + LUXURY_BRANDS,
         hours=HOURS_BODY, rating=4.4, reviewCount=298, averageWaitDays=5,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=True,
         certifications=["I-CAR Gold Class"]),
    dict(centerId="SC-CHI-104", name="AutoNation Ford — Chicago", type="dealer",
         address="2001 W Cermak Rd", city="Chicago", state="IL", zipCode="60608",
         country="USA", phone="(312) 555-2424", latitude=41.8523, longitude=-87.6759,
         capabilities=DEALER_CAPS, brandsServiced=["Ford"], hours=HOURS_DEALER,
         rating=4.2, reviewCount=921, averageWaitDays=2,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=True,
         certifications=["Ford Certified", "ASE Certified"]),
    dict(centerId="SC-CHI-105", name="Discount Tire — Chicago", type="tire-specialist",
         address="6500 W Diversey Ave", city="Chicago", state="IL", zipCode="60707",
         country="USA", phone="(773) 555-2525", latitude=41.9319, longitude=-87.7912,
         capabilities=TIRE_CAPS, brandsServiced=ALL_BRANDS + LUXURY_BRANDS,
         hours=HOURS_TIRE, rating=4.6, reviewCount=466, averageWaitDays=0,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=False,
         certifications=["TIA Certified Installer"]),
    dict(centerId="SC-CHI-106", name="Firestone Complete Auto Care — Chicago", type="independent",
         address="2050 N Clark St", city="Chicago", state="IL", zipCode="60614",
         country="USA", phone="(773) 555-2626", latitude=41.9197, longitude=-87.6363,
         capabilities=ALL_CAPS_FULL, brandsServiced=ALL_BRANDS, hours=HOURS_INDEP,
         rating=4.1, reviewCount=712, averageWaitDays=1,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=False,
         certifications=["ASE Certified"]),
    dict(centerId="SC-CHI-107", name="Jiffy Lube — Chicago", type="quick-service",
         address="3950 N Ashland Ave", city="Chicago", state="IL", zipCode="60613",
         country="USA", phone="(773) 555-2727", latitude=41.9523, longitude=-87.6692,
         capabilities=QUICK_CAPS, brandsServiced=ALL_BRANDS, hours=HOURS_QUICK,
         rating=4.0, reviewCount=389, averageWaitDays=0,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=False,
         certifications=["ASE Certified"]),

    # ------------------------------ Denver (CO) -----------------------------
    dict(centerId="SC-DEN-101", name="Pep Boys — Denver", type="independent",
         address="3100 S Federal Blvd", city="Denver", state="CO", zipCode="80236",
         country="USA", phone="(303) 555-3131", latitude=39.6644, longitude=-105.0244,
         capabilities=ALL_CAPS_FULL, brandsServiced=ALL_BRANDS, hours=HOURS_INDEP,
         rating=4.0, reviewCount=487, averageWaitDays=1,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=False,
         certifications=["ASE Certified"]),
    dict(centerId="SC-DEN-102", name="Discount Tire — Denver", type="tire-specialist",
         address="2860 S Havana St", city="Aurora", state="CO", zipCode="80014",
         country="USA", phone="(303) 555-3232", latitude=39.6680, longitude=-104.8675,
         capabilities=TIRE_CAPS, brandsServiced=ALL_BRANDS + LUXURY_BRANDS,
         hours=HOURS_TIRE, rating=4.7, reviewCount=602, averageWaitDays=0,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=False,
         certifications=["TIA Certified Installer"]),
    dict(centerId="SC-DEN-103", name="Larry H. Miller Toyota — Denver", type="dealer",
         address="4300 W 104th Ave", city="Westminster", state="CO", zipCode="80031",
         country="USA", phone="(303) 555-3333", latitude=39.8847, longitude=-105.0464,
         capabilities=DEALER_CAPS, brandsServiced=["Toyota"], hours=HOURS_DEALER,
         rating=4.5, reviewCount=974, averageWaitDays=2,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=True,
         certifications=["Toyota Certified", "ASE Certified"]),
    dict(centerId="SC-DEN-104", name="Jiffy Lube — Denver", type="quick-service",
         address="2275 S Colorado Blvd", city="Denver", state="CO", zipCode="80222",
         country="USA", phone="(303) 555-3434", latitude=39.6770, longitude=-104.9407,
         capabilities=QUICK_CAPS, brandsServiced=ALL_BRANDS, hours=HOURS_QUICK,
         rating=4.0, reviewCount=312, averageWaitDays=0,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=False,
         certifications=["ASE Certified"]),
    dict(centerId="SC-DEN-105", name="Firestone Complete Auto Care — Denver", type="independent",
         address="1490 S Colorado Blvd", city="Denver", state="CO", zipCode="80222",
         country="USA", phone="(303) 555-3535", latitude=39.6951, longitude=-104.9407,
         capabilities=ALL_CAPS_FULL, brandsServiced=ALL_BRANDS, hours=HOURS_INDEP,
         rating=4.1, reviewCount=534, averageWaitDays=1,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=False,
         certifications=["ASE Certified"]),
    dict(centerId="SC-DEN-106", name="Penske Truck Leasing — Aurora", type="fleet-service",
         address="13770 E 39th Ave", city="Aurora", state="CO", zipCode="80011",
         country="USA", phone="(303) 555-3636", latitude=39.7686, longitude=-104.7969,
         capabilities=FLEET_CAPS, brandsServiced=["Ford", "RAM", "GMC", "Chevrolet"],
         hours=HOURS_FLEET, rating=4.3, reviewCount=189, averageWaitDays=1,
         mobileServiceAvailable=True, fleetDiscount=True, loanerVehicles=False,
         certifications=["DOT Inspection Authorized", "Fleet Maintenance Certified"]),

    # ------------------------------ Munich (Germany) ------------------------
    dict(centerId="SC-MUC-101", name="BMW Welt Service — München", type="dealer",
         address="Am Olympiapark 1", city="München", state="BY", zipCode="80809",
         country="Germany", phone="+49 89 1250-16001", latitude=48.1773, longitude=11.5567,
         capabilities=DEALER_CAPS, brandsServiced=["BMW"], hours=HOURS_GERMAN,
         rating=4.7, reviewCount=1428, averageWaitDays=4,
         mobileServiceAvailable=False, fleetDiscount=False, loanerVehicles=True,
         certifications=["BMW Certified", "TÜV-zertifiziert"]),
    dict(centerId="SC-MUC-102", name="Mercedes-Benz Niederlassung München", type="dealer",
         address="Arnulfstraße 61", city="München", state="BY", zipCode="80636",
         country="Germany", phone="+49 89 1551-0", latitude=48.1466, longitude=11.5403,
         capabilities=DEALER_CAPS, brandsServiced=["Mercedes-Benz"], hours=HOURS_GERMAN,
         rating=4.6, reviewCount=1156, averageWaitDays=3,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=True,
         certifications=["Mercedes-Benz Certified", "TÜV-zertifiziert"]),
    dict(centerId="SC-MUC-103", name="Audi Zentrum München", type="dealer",
         address="Maria-Probst-Straße 21", city="München", state="BY", zipCode="80939",
         country="Germany", phone="+49 89 36087-0", latitude=48.1857, longitude=11.6019,
         capabilities=DEALER_CAPS, brandsServiced=["Audi"], hours=HOURS_GERMAN,
         rating=4.5, reviewCount=987, averageWaitDays=3,
         mobileServiceAvailable=False, fleetDiscount=False, loanerVehicles=True,
         certifications=["Audi Certified", "TÜV-zertifiziert"]),
    dict(centerId="SC-MUC-104", name="AutoHaus Volkswagen — München", type="dealer",
         address="Ingolstädter Straße 87", city="München", state="BY", zipCode="80939",
         country="Germany", phone="+49 89 31775-0", latitude=48.1965, longitude=11.5879,
         capabilities=DEALER_CAPS, brandsServiced=["Volkswagen"], hours=HOURS_GERMAN,
         rating=4.3, reviewCount=741, averageWaitDays=2,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=True,
         certifications=["Volkswagen Certified", "TÜV-zertifiziert"]),
    dict(centerId="SC-MUC-105", name="ATU Auto-Teile-Unger — München Nord", type="independent",
         address="Schleißheimer Straße 408", city="München", state="BY", zipCode="80809",
         country="Germany", phone="+49 89 35803-0", latitude=48.1986, longitude=11.5680,
         capabilities=ALL_CAPS_FULL, brandsServiced=ALL_BRANDS + LUXURY_BRANDS,
         hours=HOURS_GERMAN, rating=4.0, reviewCount=523, averageWaitDays=1,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=False,
         certifications=["TÜV-zertifiziert"]),
    dict(centerId="SC-MUC-106", name="Pirelli Reifen Service — München", type="tire-specialist",
         address="Landsberger Straße 234", city="München", state="BY", zipCode="80687",
         country="Germany", phone="+49 89 5404-7800", latitude=48.1410, longitude=11.5025,
         capabilities=TIRE_CAPS, brandsServiced=ALL_BRANDS + LUXURY_BRANDS,
         hours=HOURS_GERMAN, rating=4.5, reviewCount=412, averageWaitDays=0,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=False,
         certifications=["Pirelli Performance Center"]),
    dict(centerId="SC-MUC-107", name="Karosseriebau Schmidt — München", type="body-shop",
         address="Truderinger Straße 290", city="München", state="BY", zipCode="81825",
         country="Germany", phone="+49 89 4308-0", latitude=48.1247, longitude=11.6707,
         capabilities=BODY_CAPS, brandsServiced=ALL_BRANDS + LUXURY_BRANDS,
         hours=HOURS_BODY, rating=4.6, reviewCount=234, averageWaitDays=6,
         mobileServiceAvailable=False, fleetDiscount=True, loanerVehicles=True,
         certifications=["I-CAR Gold Class", "TÜV-zertifiziert"]),
]
# fmt: on


def _to_ddb_safe(value):
    """Recursively convert floats to Decimal so DynamoDB accepts them."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_ddb_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_ddb_safe(v) for v in value]
    return value


def main():
    table = boto3.resource("dynamodb", region_name=_REGION).Table(_TABLE_NAME)
    written = 0
    for entry in NEW_CENTERS:
        item = _to_ddb_safe(entry)
        table.put_item(Item=item)
        written += 1
    print(f"Wrote {written} centers across 6 simulator cities (Seattle, "
          f"Atlanta, Dallas, Chicago, Denver, Munich).")


if __name__ == "__main__":
    main()
