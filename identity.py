#!/usr/bin/env python3
"""Random but plausible US identities, one per registration run.

Every run must look like a different person: name, phone, date of birth and
home location all vary, and each field has to be individually believable
because the registration widgets validate them (real NANP area codes, cities
that radar.io autocomplete can actually resolve).
"""
import datetime as dt
import random

FIRST_NAMES = [
    "James", "Robert", "John", "Michael", "David", "William", "Richard", "Joseph",
    "Thomas", "Christopher", "Charles", "Daniel", "Matthew", "Anthony", "Mark",
    "Donald", "Steven", "Andrew", "Paul", "Joshua", "Kenneth", "Kevin", "Brian",
    "George", "Timothy", "Ronald", "Jason", "Edward", "Jeffrey", "Ryan", "Jacob",
    "Gary", "Nicholas", "Eric", "Jonathan", "Stephen", "Larry", "Justin", "Scott",
    "Brandon", "Benjamin", "Samuel", "Gregory", "Alexander", "Patrick", "Frank",
    "Raymond", "Jack", "Dennis", "Jerry", "Tyler", "Aaron", "Jose", "Adam", "Nathan",
    "Henry", "Zachary", "Douglas", "Peter", "Kyle", "Noah", "Ethan", "Jeremy",
    "Walter", "Christian", "Keith", "Roger", "Terry", "Austin", "Sean", "Gerald",
    "Carl", "Harold", "Dylan", "Arthur", "Lawrence", "Jordan", "Jesse", "Bryan",
    "Billy", "Bruce", "Gabriel", "Joe", "Logan", "Alan", "Juan", "Albert", "Willie",
    "Elijah", "Wayne", "Randy", "Vincent", "Mason", "Roy", "Ralph", "Bobby", "Russell",
    "Bradley", "Philip", "Eugene",
    "Mary", "Patricia", "Jennifer", "Linda", "Elizabeth", "Barbara", "Susan",
    "Jessica", "Sarah", "Karen", "Nancy", "Lisa", "Margaret", "Betty", "Sandra",
    "Ashley", "Dorothy", "Kimberly", "Emily", "Donna", "Michelle", "Carol", "Amanda",
    "Melissa", "Deborah", "Stephanie", "Rebecca", "Sharon", "Laura", "Cynthia",
    "Amy", "Kathleen", "Angela", "Shirley", "Brenda", "Emma", "Anna", "Pamela",
    "Nicole", "Samantha", "Katherine", "Christine", "Helen", "Debra", "Rachel",
    "Carolyn", "Janet", "Maria", "Catherine", "Heather", "Diane", "Olivia", "Julie",
    "Joyce", "Victoria", "Ruth", "Virginia", "Lauren", "Kelly", "Christina", "Joan",
    "Evelyn", "Judith", "Megan", "Andrea", "Cheryl", "Hannah", "Jacqueline", "Martha",
    "Gloria", "Teresa", "Ann", "Sara", "Madison", "Frances", "Kathryn", "Janice",
    "Jean", "Abigail", "Alice", "Julia", "Judy", "Sophia", "Grace", "Denise",
    "Amber", "Doris", "Marilyn", "Danielle", "Beverly", "Isabella", "Theresa",
    "Diana", "Natalie", "Brittany", "Charlotte", "Marie", "Kayla", "Alexis",
    "Lori", "Bonnie", "Rose", "Tiffany", "Veronica", "Dawn", "Erin", "Stacy",
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
    "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
    "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker",
    "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores",
    "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell",
    "Carter", "Roberts", "Gomez", "Phillips", "Evans", "Turner", "Diaz", "Parker",
    "Cruz", "Edwards", "Collins", "Reyes", "Stewart", "Morris", "Morales", "Murphy",
    "Cook", "Rogers", "Gutierrez", "Ortiz", "Morgan", "Cooper", "Peterson", "Bailey",
    "Reed", "Kelly", "Howard", "Ramos", "Kim", "Cox", "Ward", "Richardson", "Watson",
    "Brooks", "Chavez", "Wood", "James", "Bennett", "Gray", "Mendoza", "Ruiz",
    "Hughes", "Price", "Alvarez", "Castillo", "Sanders", "Patel", "Myers", "Long",
    "Ross", "Foster", "Jimenez", "Powell", "Jenkins", "Perry", "Russell", "Sullivan",
    "Bell", "Coleman", "Butler", "Henderson", "Barnes", "Gonzales", "Fisher",
    "Vasquez", "Simmons", "Romero", "Jordan", "Patterson", "Alexander", "Hamilton",
    "Graham", "Reynolds", "Griffin", "Wallace", "Moreno", "West", "Cole", "Hayes",
    "Bryant", "Herrera", "Gibson", "Ellis", "Tran", "Medina", "Aguilar", "Stevens",
    "Murray", "Ford", "Castro", "Marshall", "Owens", "Harrison", "Fernandez",
    "McDonald", "Woods", "Washington", "Kennedy", "Wells", "Vargas", "Henry",
    "Chen", "Freeman", "Webb", "Tucker", "Guzman", "Burns", "Crawford", "Olson",
    "Simpson", "Porter", "Hunter", "Gordon", "Mendez", "Silva", "Shaw", "Snyder",
    "Mason", "Dixon", "Munoz", "Hunt", "Hicks", "Holmes", "Palmer", "Wagner",
    "Black", "Robertson", "Boyd", "Rose", "Stone", "Salazar", "Fox", "Warren",
    "Mills", "Meyer", "Rice", "Schmidt", "Garza", "Daniels", "Ferguson", "Nichols",
]

# (city, state abbr, state full, lat, lon) - real US metros, radar.io resolvable.
CITIES = [
    ("New York", "NY", "New York", 40.71427, -74.00597),
    ("Los Angeles", "CA", "California", 34.05223, -118.24368),
    ("Chicago", "IL", "Illinois", 41.87811, -87.62980),
    ("Houston", "TX", "Texas", 29.76328, -95.36327),
    ("Phoenix", "AZ", "Arizona", 33.44838, -112.07404),
    ("Philadelphia", "PA", "Pennsylvania", 39.95258, -75.16522),
    ("San Antonio", "TX", "Texas", 29.42412, -98.49363),
    ("San Diego", "CA", "California", 32.71533, -117.15726),
    ("Dallas", "TX", "Texas", 32.78306, -96.80667),
    ("San Jose", "CA", "California", 37.33939, -121.89496),
    ("Austin", "TX", "Texas", 30.26715, -97.74306),
    ("Jacksonville", "FL", "Florida", 30.33218, -81.65565),
    ("Columbus", "OH", "Ohio", 39.96118, -82.99879),
    ("Charlotte", "NC", "North Carolina", 35.22709, -80.84313),
    ("Indianapolis", "IN", "Indiana", 39.76838, -86.15804),
    ("Seattle", "WA", "Washington", 47.60621, -122.33207),
    ("Denver", "CO", "Colorado", 39.73915, -104.98470),
    ("Boston", "MA", "Massachusetts", 42.35843, -71.05977),
    ("Nashville", "TN", "Tennessee", 36.16589, -86.78444),
    ("Portland", "OR", "Oregon", 45.52345, -122.67621),
    ("Las Vegas", "NV", "Nevada", 36.17497, -115.13722),
    ("Detroit", "MI", "Michigan", 42.33143, -83.04575),
    ("Memphis", "TN", "Tennessee", 35.14953, -90.04898),
    ("Louisville", "KY", "Kentucky", 38.25266, -85.75846),
    ("Baltimore", "MD", "Maryland", 39.29038, -76.61219),
    ("Milwaukee", "WI", "Wisconsin", 43.03890, -87.90647),
    ("Albuquerque", "NM", "New Mexico", 35.08449, -106.65114),
    ("Tucson", "AZ", "Arizona", 32.22174, -110.92648),
    ("Fresno", "CA", "California", 36.74773, -119.77237),
    ("Sacramento", "CA", "California", 38.58157, -121.49440),
    ("Kansas City", "MO", "Missouri", 39.09973, -94.57857),
    ("Atlanta", "GA", "Georgia", 33.74900, -84.38798),
    ("Miami", "FL", "Florida", 25.77427, -80.19366),
    ("Raleigh", "NC", "North Carolina", 35.77210, -78.63861),
    ("Omaha", "NE", "Nebraska", 41.25861, -95.93779),
    ("Minneapolis", "MN", "Minnesota", 44.97997, -93.26384),
    ("Cleveland", "OH", "Ohio", 41.49950, -81.69541),
    ("Tampa", "FL", "Florida", 27.94752, -82.45843),
    ("Pittsburgh", "PA", "Pennsylvania", 40.44062, -79.99589),
    ("St. Louis", "MO", "Missouri", 38.62727, -90.19789),
    ("Cincinnati", "OH", "Ohio", 39.12711, -84.51439),
    ("Orlando", "FL", "Florida", 28.53834, -81.37924),
    ("Richmond", "VA", "Virginia", 37.55376, -77.46026),
    ("Buffalo", "NY", "New York", 42.88645, -78.87837),
    ("Salt Lake City", "UT", "Utah", 40.76078, -111.89105),
    ("Boise", "ID", "Idaho", 43.61350, -116.20345),
    ("Tulsa", "OK", "Oklahoma", 36.15398, -95.99277),
    ("Charleston", "SC", "South Carolina", 32.77647, -79.93101),
    ("Providence", "RI", "Rhode Island", 41.82399, -71.41283),
    ("Spokane", "WA", "Washington", 47.65878, -117.42605),
    ("Des Moines", "IA", "Iowa", 41.60054, -93.60911),
    ("Little Rock", "AR", "Arkansas", 34.74648, -92.28959),
    ("Hartford", "CT", "Connecticut", 41.76371, -72.68509),
    ("New Orleans", "LA", "Louisiana", 29.95465, -90.07507),
    ("Lexington", "KY", "Kentucky", 38.04058, -84.50372),
    ("Anchorage", "AK", "Alaska", 61.21806, -149.90028),
    ("Honolulu", "HI", "Hawaii", 21.30694, -157.85833),
    ("Madison", "WI", "Wisconsin", 43.07305, -89.40123),
    ("Reno", "NV", "Nevada", 39.52963, -119.81380),
    ("Colorado Springs", "CO", "Colorado", 38.83388, -104.82136),
]

# Real NANP area codes. Excludes 555/8xx/9xx (reserved or toll-free).
AREA_CODES = [
    "201", "202", "203", "205", "206", "207", "208", "209", "210", "212",
    "213", "214", "215", "216", "217", "218", "219", "224", "225", "228",
    "229", "231", "234", "239", "240", "248", "251", "252", "253", "254",
    "256", "260", "262", "267", "269", "270", "276", "281", "301", "302",
    "303", "304", "305", "307", "308", "309", "310", "312", "313", "314",
    "315", "316", "317", "318", "319", "320", "321", "323", "325", "330",
    "331", "334", "336", "337", "339", "346", "347", "351", "352", "360",
    "361", "364", "380", "386", "401", "402", "404", "405", "406", "407",
    "408", "409", "410", "412", "413", "414", "415", "417", "419", "423",
    "424", "425", "430", "432", "434", "435", "440", "442", "443", "469",
    "470", "475", "478", "479", "480", "484", "501", "502", "503", "504",
    "505", "507", "508", "509", "510", "512", "513", "515", "516", "517",
    "518", "520", "530", "540", "541", "551", "559", "561", "562", "563",
    "567", "570", "571", "573", "574", "580", "585", "586", "601", "602",
    "603", "605", "606", "607", "608", "609", "610", "612", "614", "615",
    "616", "617", "618", "619", "620", "623", "626", "628", "629", "630",
    "631", "636", "641", "646", "650", "651", "657", "660", "661", "662",
    "667", "678", "681", "682", "701", "702", "703", "704", "706", "707",
    "708", "712", "713", "714", "715", "716", "717", "718", "719", "720",
    "724", "725", "727", "731", "732", "734", "737", "740", "747", "754",
    "757", "760", "762", "763", "765", "769", "770", "772", "773", "774",
    "775", "779", "781", "785", "786", "801", "802", "803", "804", "805",
    "806", "808", "810", "812", "813", "814", "815", "816", "817", "818",
    "828", "830", "831", "832", "843", "845", "847", "848", "850", "856",
    "857", "858", "859", "860", "862", "863", "864", "865", "870", "872",
    "878", "901", "903", "904", "906", "907", "908", "909", "910", "912",
    "913", "914", "915", "916", "917", "918", "919", "920", "925", "928",
    "929", "930", "931", "936", "937", "940", "941", "947", "949", "951",
    "952", "954", "956", "959", "970", "971", "972", "973", "978", "979",
    "980", "984", "985", "989",
]

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def random_phone(rnd):
    """NANP: NPA-NXX-XXXX with NXX starting 2-9 and not ending 11."""
    area = rnd.choice(AREA_CODES)
    while True:
        exchange = f"{rnd.randint(2, 9)}{rnd.randint(0, 9)}{rnd.randint(0, 9)}"
        if not exchange.endswith("11"):
            break
    line = f"{rnd.randint(0, 9)}{rnd.randint(0, 9)}{rnd.randint(0, 9)}{rnd.randint(0, 9)}"
    return area + exchange + line


def random_identity(rnd=None, min_age=18, max_age=58):
    """One complete, self-consistent person."""
    rnd = rnd or random.SystemRandom()

    # date of birth: comfortably above the 18+ gate, birthday already passed
    today = dt.date.today()
    year = rnd.randint(today.year - max_age, today.year - min_age)
    month = rnd.randint(1, 12)
    day = rnd.randint(1, 28)
    dob = dt.date(year, month, day)

    city, abbr, state_full, lat, lon = rnd.choice(CITIES)
    # jitter the coordinates a little so two people in one city differ
    lat = round(lat + rnd.uniform(-0.05, 0.05), 5)
    lon = round(lon + rnd.uniform(-0.05, 0.05), 5)

    return {
        "first_name": rnd.choice(FIRST_NAMES),
        "last_name": rnd.choice(LAST_NAMES),
        "phone": random_phone(rnd),
        "dob_day": dob.day,
        "dob_month": MONTHS[dob.month - 1],
        "dob_year": dob.year,
        "date_of_birth": dob.isoformat(),
        "location": {
            "latitude": lat,
            "longitude": lon,
            "city": city,
            "state": state_full,
            "country": "United States",
            "countryCode": "US",
        },
        "location_query": f"{city}, {abbr}",
        "location_pick": f"{city}, {state_full}, United States",
    }


def identity_key(ident):
    """The tuple that must stay unique across the database."""
    return (ident["first_name"].lower(), ident["last_name"].lower(),
            ident["phone"])


if __name__ == "__main__":
    import json
    rnd = random.SystemRandom()
    for _ in range(3):
        print(json.dumps(random_identity(rnd), indent=1))
