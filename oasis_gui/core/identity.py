#!/usr/bin/env python3
"""Random but plausible US / UK identities, one per registration run.

Every run must look like a different person: name, phone, date of birth and
home location all vary, and each field has to be individually believable
because the registration widgets validate them (real NANP area codes for US
numbers, real 07xxx mobile ranges for UK, cities that autocomplete resolves).
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
    # common in the UK, keeps a GB address from looking American
    "Walsh", "O'Brien", "O'Connor", "Byrne", "Gallagher", "Doyle", "McCarthy",
    "Quinn", "Kavanagh", "Nolan", "Brennan", "Fitzgerald", "Murray", "Reid",
    "Shaw", "Duncan", "Fraser", "MacLeod", "Sinclair", "Cameron", "Watts",
    "Whitfield", "Ashworth", "Bancroft", "Chadwick", "Fairbrother", "Grimshaw",
]

# (city, state abbr, region full, country, country code, lat, lon)
US_CITIES = [
    ("New York", "NY", "New York", "United States", "US", 40.71427, -74.00597),
    ("Los Angeles", "CA", "California", "United States", "US", 34.05223, -118.24368),
    ("Chicago", "IL", "Illinois", "United States", "US", 41.87811, -87.62980),
    ("Houston", "TX", "Texas", "United States", "US", 29.76328, -95.36327),
    ("Phoenix", "AZ", "Arizona", "United States", "US", 33.44838, -112.07404),
    ("Philadelphia", "PA", "Pennsylvania", "United States", "US", 39.95258, -75.16522),
    ("San Antonio", "TX", "Texas", "United States", "US", 29.42412, -98.49363),
    ("San Diego", "CA", "California", "United States", "US", 32.71533, -117.15726),
    ("Dallas", "TX", "Texas", "United States", "US", 32.78306, -96.80667),
    ("San Jose", "CA", "California", "United States", "US", 37.33939, -121.89496),
    ("Austin", "TX", "Texas", "United States", "US", 30.26715, -97.74306),
    ("Jacksonville", "FL", "Florida", "United States", "US", 30.33218, -81.65565),
    ("Columbus", "OH", "Ohio", "United States", "US", 39.96118, -82.99879),
    ("Charlotte", "NC", "North Carolina", "United States", "US", 35.22709, -80.84313),
    ("Indianapolis", "IN", "Indiana", "United States", "US", 39.76838, -86.15804),
    ("Seattle", "WA", "Washington", "United States", "US", 47.60621, -122.33207),
    ("Denver", "CO", "Colorado", "United States", "US", 39.73915, -104.98470),
    ("Boston", "MA", "Massachusetts", "United States", "US", 42.35843, -71.05977),
    ("Nashville", "TN", "Tennessee", "United States", "US", 36.16589, -86.78444),
    ("Portland", "OR", "Oregon", "United States", "US", 45.52345, -122.67621),
    ("Las Vegas", "NV", "Nevada", "United States", "US", 36.17497, -115.13722),
    ("Detroit", "MI", "Michigan", "United States", "US", 42.33143, -83.04575),
    ("Memphis", "TN", "Tennessee", "United States", "US", 35.14953, -90.04898),
    ("Louisville", "KY", "Kentucky", "United States", "US", 38.25266, -85.75846),
    ("Baltimore", "MD", "Maryland", "United States", "US", 39.29038, -76.61219),
    ("Milwaukee", "WI", "Wisconsin", "United States", "US", 43.03890, -87.90647),
    ("Albuquerque", "NM", "New Mexico", "United States", "US", 35.08449, -106.65114),
    ("Tucson", "AZ", "Arizona", "United States", "US", 32.22174, -110.92648),
    ("Fresno", "CA", "California", "United States", "US", 36.74773, -119.77237),
    ("Sacramento", "CA", "California", "United States", "US", 38.58157, -121.49440),
    ("Kansas City", "MO", "Missouri", "United States", "US", 39.09973, -94.57857),
    ("Atlanta", "GA", "Georgia", "United States", "US", 33.74900, -84.38798),
    ("Miami", "FL", "Florida", "United States", "US", 25.77427, -80.19366),
    ("Raleigh", "NC", "North Carolina", "United States", "US", 35.77210, -78.63861),
    ("Omaha", "NE", "Nebraska", "United States", "US", 41.25861, -95.93779),
    ("Minneapolis", "MN", "Minnesota", "United States", "US", 44.97997, -93.26384),
    ("Cleveland", "OH", "Ohio", "United States", "US", 41.49950, -81.69541),
    ("Tampa", "FL", "Florida", "United States", "US", 27.94752, -82.45843),
    ("Pittsburgh", "PA", "Pennsylvania", "United States", "US", 40.44062, -79.99589),
    ("St. Louis", "MO", "Missouri", "United States", "US", 38.62727, -90.19789),
    ("Cincinnati", "OH", "Ohio", "United States", "US", 39.12711, -84.51439),
    ("Orlando", "FL", "Florida", "United States", "US", 28.53834, -81.37924),
    ("Richmond", "VA", "Virginia", "United States", "US", 37.55376, -77.46026),
    ("Buffalo", "NY", "New York", "United States", "US", 42.88645, -78.87837),
    ("Salt Lake City", "UT", "Utah", "United States", "US", 40.76078, -111.89105),
    ("Boise", "ID", "Idaho", "United States", "US", 43.61350, -116.20345),
    ("Tulsa", "OK", "Oklahoma", "United States", "US", 36.15398, -95.99277),
    ("Charleston", "SC", "South Carolina", "United States", "US", 32.77647, -79.93101),
    ("Providence", "RI", "Rhode Island", "United States", "US", 41.82399, -71.41283),
    ("Spokane", "WA", "Washington", "United States", "US", 47.65878, -117.42605),
    ("Des Moines", "IA", "Iowa", "United States", "US", 41.60054, -93.60911),
    ("Little Rock", "AR", "Arkansas", "United States", "US", 34.74648, -92.28959),
    ("Hartford", "CT", "Connecticut", "United States", "US", 41.76371, -72.68509),
    ("New Orleans", "LA", "Louisiana", "United States", "US", 29.95465, -90.07507),
    ("Lexington", "KY", "Kentucky", "United States", "US", 38.04058, -84.50372),
    ("Anchorage", "AK", "Alaska", "United States", "US", 61.21806, -149.90028),
    ("Honolulu", "HI", "Hawaii", "United States", "US", 21.30694, -157.85833),
    ("Madison", "WI", "Wisconsin", "United States", "US", 43.07305, -89.40123),
    ("Reno", "NV", "Nevada", "United States", "US", 39.52963, -119.81380),
    ("Colorado Springs", "CO", "Colorado", "United States", "US", 38.83388, -104.82136),
]

# The pool the operator asked for. An identity is drawn from the country the
# proxy actually exits in, when that country is in here; otherwise it falls back
# to a random pick from the pool (and says so in the log).
SUPPORTED_COUNTRIES = ("US", "DE", "FR")

# (city, region/state, country, country code, lat, lon)
DE_CITIES = [
    ("Berlin", "Berlin", "Germany", "DE", 52.52437, 13.41053),
    ("Hamburg", "Hamburg", "Germany", "DE", 53.55073, 9.99302),
    ("Munich", "Bavaria", "Germany", "DE", 48.13743, 11.57549),
    ("Cologne", "North Rhine-Westphalia", "Germany", "DE", 50.93333, 6.95),
    ("Frankfurt am Main", "Hesse", "Germany", "DE", 50.11552, 8.68417),
    ("Stuttgart", "Baden-Wuerttemberg", "Germany", "DE", 48.78232, 9.17702),
    ("Duesseldorf", "North Rhine-Westphalia", "Germany", "DE", 51.22172, 6.77616),
    ("Leipzig", "Saxony", "Germany", "DE", 51.33962, 12.37129),
    ("Dortmund", "North Rhine-Westphalia", "Germany", "DE", 51.51494, 7.466),
    ("Essen", "North Rhine-Westphalia", "Germany", "DE", 51.45657, 7.01228),
    ("Bremen", "Bremen", "Germany", "DE", 53.07516, 8.80777),
    ("Dresden", "Saxony", "Germany", "DE", 51.05089, 13.73832),
    ("Hanover", "Lower Saxony", "Germany", "DE", 52.37052, 9.73322),
    ("Nuremberg", "Bavaria", "Germany", "DE", 49.45421, 11.07752),
    ("Duisburg", "North Rhine-Westphalia", "Germany", "DE", 51.43247, 6.76516),
    ("Bochum", "North Rhine-Westphalia", "Germany", "DE", 51.48165, 7.21648),
    ("Wuppertal", "North Rhine-Westphalia", "Germany", "DE", 51.27063, 7.16755),
    ("Bielefeld", "North Rhine-Westphalia", "Germany", "DE", 52.03333, 8.53333),
    ("Bonn", "North Rhine-Westphalia", "Germany", "DE", 50.73438, 7.09549),
    ("Muenster", "North Rhine-Westphalia", "Germany", "DE", 51.96236, 7.62571),
    ("Karlsruhe", "Baden-Wuerttemberg", "Germany", "DE", 49.00937, 8.40444),
    ("Mannheim", "Baden-Wuerttemberg", "Germany", "DE", 49.4891, 8.46694),
    ("Augsburg", "Bavaria", "Germany", "DE", 48.36882, 10.89779),
    ("Wiesbaden", "Hesse", "Germany", "DE", 50.08258, 8.24932),
    ("Braunschweig", "Lower Saxony", "Germany", "DE", 52.26594, 10.52673),
    ("Chemnitz", "Saxony", "Germany", "DE", 50.8357, 12.92922),
    ("Kiel", "Schleswig-Holstein", "Germany", "DE", 54.32133, 10.13489),
    ("Aachen", "North Rhine-Westphalia", "Germany", "DE", 50.77664, 6.08342),
    ("Magdeburg", "Saxony-Anhalt", "Germany", "DE", 52.12773, 11.62916),
    ("Freiburg", "Baden-Wuerttemberg", "Germany", "DE", 47.9959, 7.85222),
    ("Mainz", "Rhineland-Palatinate", "Germany", "DE", 49.98419, 8.2791),
    ("Luebeck", "Schleswig-Holstein", "Germany", "DE", 53.86893, 10.68729),
    ("Erfurt", "Thuringia", "Germany", "DE", 50.9787, 11.03283),
    ("Rostock", "Mecklenburg-Vorpommern", "Germany", "DE", 54.0887, 12.14049),
    ("Kassel", "Hesse", "Germany", "DE", 51.31667, 9.5),
    ("Potsdam", "Brandenburg", "Germany", "DE", 52.39886, 13.06566),
    ("Saarbruecken", "Saarland", "Germany", "DE", 49.23262, 6.99694),
    ("Heidelberg", "Baden-Wuerttemberg", "Germany", "DE", 49.40768, 8.69079),
    ("Regensburg", "Bavaria", "Germany", "DE", 49.01513, 12.10161),
    ("Ulm", "Baden-Wuerttemberg", "Germany", "DE", 48.39841, 9.99155),
]

FR_CITIES = [
    ("Paris", "Ile-de-France", "France", "FR", 48.85341, 2.3488),
    ("Marseille", "Provence-Alpes-Cote d'Azur", "France", "FR", 43.29551, 5.38958),
    ("Lyon", "Auvergne-Rhone-Alpes", "France", "FR", 45.74846, 4.84671),
    ("Toulouse", "Occitanie", "France", "FR", 43.60426, 1.44422),
    ("Nice", "Provence-Alpes-Cote d'Azur", "France", "FR", 43.70313, 7.26608),
    ("Nantes", "Pays de la Loire", "France", "FR", 47.21725, -1.55336),
    ("Montpellier", "Occitanie", "France", "FR", 43.61093, 3.87635),
    ("Strasbourg", "Grand Est", "France", "FR", 48.58392, 7.74553),
    ("Bordeaux", "Nouvelle-Aquitaine", "France", "FR", 44.84044, -0.5805),
    ("Lille", "Hauts-de-France", "France", "FR", 50.63297, 3.05858),
    ("Rennes", "Brittany", "France", "FR", 48.11198, -1.67429),
    ("Reims", "Grand Est", "France", "FR", 49.26526, 4.02853),
    ("Le Havre", "Normandy", "France", "FR", 49.49346, 0.10785),
    ("Saint-Etienne", "Auvergne-Rhone-Alpes", "France", "FR", 45.43389, 4.39),
    ("Toulon", "Provence-Alpes-Cote d'Azur", "France", "FR", 43.12442, 5.92836),
    ("Grenoble", "Auvergne-Rhone-Alpes", "France", "FR", 45.16667, 5.71667),
    ("Dijon", "Bourgogne-Franche-Comte", "France", "FR", 47.31667, 5.01667),
    ("Angers", "Pays de la Loire", "France", "FR", 47.47156, -0.55202),
    ("Nimes", "Occitanie", "France", "FR", 43.83665, 4.35788),
    ("Villeurbanne", "Auvergne-Rhone-Alpes", "France", "FR", 45.76667, 4.88333),
    ("Clermont-Ferrand", "Auvergne-Rhone-Alpes", "France", "FR", 45.77969, 3.08682),
    ("Le Mans", "Pays de la Loire", "France", "FR", 48.00039, 0.20471),
    ("Aix-en-Provence", "Provence-Alpes-Cote d'Azur", "France", "FR", 43.5283, 5.44973),
    ("Brest", "Brittany", "France", "FR", 48.39029, -4.48628),
    ("Tours", "Centre-Val de Loire", "France", "FR", 47.39484, 0.70398),
    ("Amiens", "Hauts-de-France", "France", "FR", 49.9, 2.3),
    ("Limoges", "Nouvelle-Aquitaine", "France", "FR", 45.83362, 1.24759),
    ("Annecy", "Auvergne-Rhone-Alpes", "France", "FR", 45.90878, 6.12565),
    ("Perpignan", "Occitanie", "France", "FR", 42.69764, 2.89541),
    ("Besancon", "Bourgogne-Franche-Comte", "France", "FR", 47.24878, 6.01815),
    ("Metz", "Grand Est", "France", "FR", 49.11911, 6.17269),
    ("Orleans", "Centre-Val de Loire", "France", "FR", 47.90289, 1.90389),
    ("Rouen", "Normandy", "France", "FR", 49.44313, 1.09932),
    ("Mulhouse", "Grand Est", "France", "FR", 47.75201, 7.32866),
    ("Caen", "Normandy", "France", "FR", 49.18585, -0.35912),
    ("Nancy", "Grand Est", "France", "FR", 48.68439, 6.18496),
    ("Avignon", "Provence-Alpes-Cote d'Azur", "France", "FR", 43.94834, 4.80892),
    ("Poitiers", "Nouvelle-Aquitaine", "France", "FR", 46.58022, 0.34037),
    ("Dunkerque", "Hauts-de-France", "France", "FR", 51.0344, 2.37675),
    ("Versailles", "Ile-de-France", "France", "FR", 48.80359, 2.13424),
]

UK_CITIES = [
    ("London", "", "England", "United Kingdom", "GB", 51.50736, -0.12759),
    ("Birmingham", "", "England", "United Kingdom", "GB", 52.48624, -1.89040),
    ("Manchester", "", "England", "United Kingdom", "GB", 53.48095, -2.23743),
    ("Leeds", "", "England", "United Kingdom", "GB", 53.80075, -1.54908),
    ("Liverpool", "", "England", "United Kingdom", "GB", 53.41058, -2.97794),
    ("Sheffield", "", "England", "United Kingdom", "GB", 53.38297, -1.46590),
    ("Bristol", "", "England", "United Kingdom", "GB", 51.45451, -2.58791),
    ("Newcastle upon Tyne", "", "England", "United Kingdom", "GB", 54.97328, -1.61396),
    ("Nottingham", "", "England", "United Kingdom", "GB", 52.95360, -1.15047),
    ("Leicester", "", "England", "United Kingdom", "GB", 52.63860, -1.13169),
    ("Southampton", "", "England", "United Kingdom", "GB", 50.90395, -1.40428),
    ("Portsmouth", "", "England", "United Kingdom", "GB", 50.79899, -1.09125),
    ("Brighton", "", "England", "United Kingdom", "GB", 50.82838, -0.13947),
    ("Oxford", "", "England", "United Kingdom", "GB", 51.75222, -1.25772),
    ("Cambridge", "", "England", "United Kingdom", "GB", 52.20000, 0.11667),
    ("York", "", "England", "United Kingdom", "GB", 53.95763, -1.08271),
    ("Norwich", "", "England", "United Kingdom", "GB", 52.62783, 1.29834),
    ("Plymouth", "", "England", "United Kingdom", "GB", 50.37153, -4.14305),
    ("Coventry", "", "England", "United Kingdom", "GB", 52.40656, -1.51217),
    ("Reading", "", "England", "United Kingdom", "GB", 51.45625, -0.97113),
    ("Milton Keynes", "", "England", "United Kingdom", "GB", 52.04062, -0.75942),
    ("Edinburgh", "", "Scotland", "United Kingdom", "GB", 55.95325, -3.18827),
    ("Glasgow", "", "Scotland", "United Kingdom", "GB", 55.86515, -4.25763),
    ("Aberdeen", "", "Scotland", "United Kingdom", "GB", 57.14369, -2.09814),
    ("Dundee", "", "Scotland", "United Kingdom", "GB", 56.46913, -2.97489),
    ("Inverness", "", "Scotland", "United Kingdom", "GB", 57.47908, -4.22398),
    ("Stirling", "", "Scotland", "United Kingdom", "GB", 56.11652, -3.93690),
    ("Cardiff", "", "Wales", "United Kingdom", "GB", 51.48000, -3.18000),
    ("Swansea", "", "Wales", "United Kingdom", "GB", 51.62079, -3.94323),
    ("Newport", "", "Wales", "United Kingdom", "GB", 51.58774, -2.99835),
    ("Wrexham", "", "Wales", "United Kingdom", "GB", 53.04659, -2.99130),
    ("Belfast", "", "Northern Ireland", "United Kingdom", "GB", 54.59682, -5.92541),
    ("Londonderry", "", "Northern Ireland", "United Kingdom", "GB", 54.99810, -7.30934),
    ("Lisburn", "", "Northern Ireland", "United Kingdom", "GB", 54.52337, -6.03527),
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


def random_phone(rnd, country="US"):
    """A plausible mobile number in E.164 *national* form (no trunk prefix).

    US  NANP: NPA-NXX-XXXX, real area codes, exchange never ends 11.
    DE  mobile ranges 015x/016x/017x -> 10 digits starting 15/16/17.
    FR  mobile 06/07            -> 9 digits starting 6/7.
    """
    if country == "DE":
        prefix = rnd.choice(["151", "152", "155", "157", "160", "162", "163",
                             "170", "171", "172", "173", "174", "175", "176",
                             "177", "178", "179"])
        return prefix + "".join(str(rnd.randint(0, 9))
                                for _ in range(10 - len(prefix)))
    if country == "FR":
        return rnd.choice(["6", "7"]) + "".join(
            str(rnd.randint(0, 9)) for _ in range(8))
    if country == "GB":
        prefix = rnd.choice(["74", "75", "76", "77", "78", "79"])
        return prefix + "".join(str(rnd.randint(0, 9)) for _ in range(8))
    area = rnd.choice(AREA_CODES)
    while True:
        exchange = f"{rnd.randint(2, 9)}{rnd.randint(0, 9)}{rnd.randint(0, 9)}"
        if not exchange.endswith("11"):
            break
    line = "".join(str(rnd.randint(0, 9)) for _ in range(4))
    return area + exchange + line


# country code -> (city list, calling code, "City, CC" query suffix,
#                  "City, Region, Country" pick suffix)
_COUNTRY_BY_CC = {"US": "United States", "DE": "Germany", "FR": "France",
                  "GB": "United Kingdom"}
_CALLING = {"US": "1", "DE": "49", "FR": "33", "GB": "44"}
_CITY_LISTS = {"US": None, "DE": None, "FR": None, "GB": None}   # filled below


def _city_list(cc):
    if cc == "US":
        return US_CITIES
    if cc == "DE":
        return DE_CITIES
    if cc == "FR":
        return FR_CITIES
    return UK_CITIES


def random_identity(rnd=None, min_age=18, max_age=58, country=None):
    """One complete, self-consistent person.

    `country` pins the locale ("US"/"DE"/"FR"). Callers should pass the country
    the proxy exits in, so the home address matches the IP the site sees; when
    it is None or unsupported a country is drawn from SUPPORTED_COUNTRIES.
    """
    rnd = rnd or random.SystemRandom()
    if country not in SUPPORTED_COUNTRIES:
        country = rnd.choice(SUPPORTED_COUNTRIES)

    today = dt.date.today()
    year = rnd.randint(today.year - max_age, today.year - min_age)
    month = rnd.randint(1, 12)
    day = rnd.randint(1, 28)
    dob = dt.date(year, month, day)

    row = rnd.choice(_city_list(country))
    if len(row) == 7:                       # US rows carry a state abbreviation
        city, abbr, region, country_name, cc, lat, lon = row
    else:
        city, region, country_name, cc, lat, lon = row
        abbr = cc
    lat = round(lat + rnd.uniform(-0.05, 0.05), 5)
    lon = round(lon + rnd.uniform(-0.05, 0.05), 5)

    query = f"{city}, {abbr}" if cc == "US" else f"{city}, {cc}"
    pick = f"{city}, {region}, {country_name}"

    return {
        "first_name": rnd.choice(FIRST_NAMES),
        "last_name": rnd.choice(LAST_NAMES),
        "phone": random_phone(rnd, cc),
        "country_calling_code": _CALLING.get(cc, "1"),
        "dob_day": dob.day,
        "dob_month": MONTHS[dob.month - 1],
        "dob_year": dob.year,
        "date_of_birth": dob.isoformat(),
        "location": {
            "latitude": lat,
            "longitude": lon,
            "city": city,
            "state": region,
            "country": country_name,
            "countryCode": cc,
        },
        "location_query": query,
        "location_pick": pick,
    }


if __name__ == "__main__":
    import json
    rnd = random.SystemRandom()
    for _ in range(3):
        print(json.dumps(random_identity(rnd), indent=1))
