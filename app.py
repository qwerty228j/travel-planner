from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from models import db, User, Trip, TripParticipant, Expense, Debt, Task, Checklist, Place, Notification, Invitation, Receipt, ReceiptItem
from datetime import datetime
import hashlib, os, re, json, urllib.request, urllib.parse
from math import radians, sin, cos, sqrt, atan2
from dotenv import load_dotenv
from abc import ABC, abstractmethod

import pytesseract
from PIL import Image, ImageOps, ImageEnhance, ImageFilter
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'default-secret-key')
app.config['PROPAGATE_EXCEPTIONS'] = True

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'receipts')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024

DB_USER = os.getenv('DB_USER', 'postgres')
DB_PASSWORD = os.getenv('DB_PASSWORD', '')
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('DB_NAME', 'travel_planner')

app.config['SQLALCHEMY_DATABASE_URI'] = f'postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)


class DatabaseManager:
    _instance = None
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    def __init__(self):
        if self._initialized: return
        self._initialized = True
        print("✅ DatabaseManager создан (Singleton)")
db_singleton = DatabaseManager()


class Command(ABC):
    @abstractmethod
    def execute(self): pass

class CreateTripCommand(Command):
    def __init__(self, trip_data, user_id):
        self.trip_data = trip_data; self.user_id = user_id
    def execute(self):
        trip = Trip(
            name=self.trip_data['name'],
            destination=self.trip_data.get('destination'),
            start_date=datetime.strptime(self.trip_data['start_date'], '%Y-%m-%d').date() if self.trip_data.get('start_date') else None,
            end_date=datetime.strptime(self.trip_data['end_date'], '%Y-%m-%d').date() if self.trip_data.get('end_date') else None,
            budget=self.trip_data.get('budget'),
            created_by=self.user_id
        )
        db.session.add(trip); db.session.flush()
        db.session.add(TripParticipant(trip_id=trip.id, user_id=self.user_id, role='organizer'))
        db.session.commit()
        return {'id': trip.id, 'success': True}

class AddExpenseCommand(Command):
    def __init__(self, trip_id, expense_data, user_id):
        self.trip_id = trip_id; self.expense_data = expense_data; self.user_id = user_id
    def execute(self):
        e = Expense(
            trip_id=self.trip_id, amount=self.expense_data['amount'],
            description=self.expense_data['description'], paid_by=self.user_id,
            date=datetime.strptime(self.expense_data['date'], '%Y-%m-%d').date()
        )
        db.session.add(e); db.session.commit()
        return {'success': True, 'expense_id': e.id}

class CompleteTaskCommand(Command):
    def __init__(self, task_id): self.task_id = task_id
    def execute(self):
        t = Task.query.get(self.task_id)
        if t:
            t.is_completed = True; db.session.commit(); return {'success': True}
        return {'success': False}


class Observer:
    def update(self, event_type, data): pass

class Subject:
    def __init__(self): self._observers = []
    def attach(self, o): self._observers.append(o)
    def notify(self, event_type, data):
        for o in self._observers: o.update(event_type, data)

class NotificationObserver(Observer):
    def update(self, event_type, data):
        if event_type == 'task_created':
            for p in TripParticipant.query.filter_by(trip_id=data['trip_id']).all():
                db.session.add(Notification(
                    user_id=p.user_id, trip_id=data['trip_id'],
                    message=f'📌 Создана задача "{data["task_title"]}" со сроком до {data["deadline"]}',
                    is_read=False))
            db.session.commit()

event_manager = Subject()
event_manager.attach(NotificationObserver())


def hash_password(p): return hashlib.sha256(p.encode()).hexdigest()


def parse_place_link(url):
    result = {}
    if not url: return result
    if not url.startswith('http'): url = 'https://' + url
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36',
            'Accept-Language': 'ru,en;q=0.9'
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode('utf-8', errors='ignore')
            final_url = r.geturl()
        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html, re.IGNORECASE)
        if m: result['name'] = m.group(1).strip()[:120]
        if 'name' not in result:
            m = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
            if m: result['name'] = m.group(1).strip()[:120]
        m = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)', html, re.IGNORECASE)
        if m: result['notes'] = m.group(1).strip()[:300]
        image_url = None
        m = re.search(r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)', html, re.IGNORECASE)
        if m: image_url = m.group(1).strip()
        if image_url:
            if image_url.startswith('//'): image_url = 'https:' + image_url
            elif image_url.startswith('/'):
                parsed = urllib.parse.urlparse(final_url)
                image_url = f'{parsed.scheme}://{parsed.netloc}{image_url}'
            result['image_url'] = image_url
    except Exception as e:
        print(f"⚠️ parse_place_link: {e}")
    return result


def fetch_wikipedia_image(query):
    if not query: return None
    try:
        search_url = 'https://ru.wikipedia.org/w/api.php?' + urllib.parse.urlencode({
            'action': 'query', 'format': 'json', 'list': 'search',
            'srsearch': query, 'srlimit': 1
        })
        req = urllib.request.Request(search_url, headers={'User-Agent': 'PoehaliVmeste/1.0'})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode('utf-8'))
        results = data.get('query', {}).get('search', [])
        if not results: return None
        title = results[0]['title']
        img_url = 'https://ru.wikipedia.org/w/api.php?' + urllib.parse.urlencode({
            'action': 'query', 'format': 'json', 'prop': 'pageimages',
            'piprop': 'original|thumbnail', 'pithumbsize': 600, 'titles': title
        })
        req = urllib.request.Request(img_url, headers={'User-Agent': 'PoehaliVmeste/1.0'})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode('utf-8'))
        pages = data.get('query', {}).get('pages', {})
        for pid, page in pages.items():
            if 'thumbnail' in page:
                return page['thumbnail']['source']
    except Exception as e:
        print(f"⚠️ Wikipedia image: {e}")
    return None


def find_nearby_stops(lat, lng, radius_m=800, limit=6):
    if not lat or not lng: return []
    query = f"""
    [out:json][timeout:15];
    (
      node["highway"="bus_stop"](around:{radius_m},{lat},{lng});
      node["public_transport"="platform"](around:{radius_m},{lat},{lng});
      node["railway"="tram_stop"](around:{radius_m},{lat},{lng});
      node["railway"="station"](around:{radius_m},{lat},{lng});
      node["amenity"="bus_station"](around:{radius_m},{lat},{lng});
    );
    out center;
    """
    try:
        req = urllib.request.Request('https://overpass-api.de/api/interpreter',
            data=query.encode('utf-8'), headers={'User-Agent': 'PoehaliVmeste/1.0'})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode('utf-8'))
        stops = []
        for el in data.get('elements', []):
            s_lat = el.get('lat') or el.get('center', {}).get('lat')
            s_lng = el.get('lon') or el.get('center', {}).get('lon')
            if not s_lat or not s_lng: continue
            tags = el.get('tags', {})
            name = tags.get('name') or tags.get('name:ru') or tags.get('ref') or 'Остановка'
            types = []
            if tags.get('highway') == 'bus_stop': types.append('автобус')
            if tags.get('railway') == 'tram_stop' or tags.get('tram') == 'yes': types.append('трамвай')
            if tags.get('railway') == 'station': types.append('поезд')
            if tags.get('amenity') == 'bus_station': types.append('автовокзал')
            if not types: types.append('остановка')
            R = 6371000
            dlat = radians(s_lat - lat); dlng = radians(s_lng - lng)
            a = sin(dlat/2)**2 + cos(radians(lat)) * cos(radians(s_lat)) * sin(dlng/2)**2
            c = 2 * atan2(sqrt(a), sqrt(1-a))
            distance_m = int(R * c)
            stops.append({'name': name, 'distance_m': distance_m, 'lat': s_lat, 'lng': s_lng,
                          'types': ', '.join(sorted(set(types))), 'routes': tags.get('route_ref', '')})
        stops.sort(key=lambda s: s['distance_m'])
        return stops[:limit]
    except Exception as e:
        print(f"⚠️ Overpass API: {e}")
        return []


def _nominatim_search(query):
    try:
        url = 'https://nominatim.openstreetmap.org/search?' + urllib.parse.urlencode({
            'q': query, 'format': 'json', 'limit': 1, 'accept-language': 'ru'
        })
        req = urllib.request.Request(url, headers={'User-Agent': 'PoehaliVmeste/1.0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode('utf-8'))
            if data: return float(data[0]['lat']), float(data[0]['lon'])
    except Exception as e:
        print(f"⚠️ Nominatim '{query}': {e}")
    return None, None


def geocode_address(name=None, address=None, city_hint=None):
    variants = []
    if address: variants.append(address)
    if address and city_hint: variants.append(f"{address}, {city_hint}")
    if city_hint and not address: variants.append(city_hint)
    if name and city_hint: variants.append(f"{name}, {city_hint}")
    if name: variants.append(name)
    seen = set()
    for q in variants:
        q = q.strip()
        if not q or q.lower() in seen: continue
        seen.add(q.lower())
        lat, lng = _nominatim_search(q)
        if lat and lng:
            print(f"✅ Геокодинг '{q}': {lat}, {lng}")
            return lat, lng
    return None, None


# ============ OCR ЧЕКА ============
def preprocess_receipt_image(image_path):
    img = Image.open(image_path)
    img = ImageOps.exif_transpose(img)
    img = img.convert('L')
    w, h = img.size
    max_side = max(w, h)
    if max_side < 2000:
        scale = 2000 / max_side
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(1.8)
    img = img.filter(ImageFilter.SHARPEN)
    img = img.point(lambda x: 0 if x < 140 else 255, '1')
    return img


def parse_receipt_text(text):
    if not text:
        return []
    items = []

    price_line = re.compile(
        r'^(.+?)\s+'
        r'(?:(\d+[.,]?\d*)\s*[xхX×*]\s*)?'
        r'(\d{1,5}[.,]\d{2})\s*'
        r'(?:=\s*(\d{1,6}[.,]\d{2}))?'
        r'\s*[A-Za-zА-Яа-я]{0,3}\s*$'
    )

    stop = re.compile(
        r'(ИТОГ|ИТОГО|ВСЕГО|СУММА|К\s*ОПЛАТЕ|НАЛИЧНЫМИ|БЕЗ\s*НДС|'
        r'НДС|СДАЧА|КАРТОЙ|ЭЛЕКТРОННЫМИ|ПОЛУЧЕНО|ВНЕСЕНО|'
        r'СУММА\s*БЕЗ\s*СКИДКИ|СКИДКА|КАССИР|СМЕНА|ФД|ФН|ФП|РН\s*ККТ|'
        r'ИНН|ОФД|ЗАВОДСКОЙ|РЕГ\.?\s*НОМЕР|МЕСТО\s*РАСЧЕТОВ|САЙТ|ТЕЛ|'
        r'№\s*ЧЕКА|ЧЕК\s*№|СМЕНА\s*№|КАССА|ПРИХОД|РАСХОД|ВОЗВРАТ|'
        r'ЭКВ\.?\s*НАЛИЧ|ЭКВ\.?\s*КАРТ|ОПЛАТА|ПЛАТЕЖ|ТЕРМИНАЛ|'
        r'ШТРИХ|QR|КОД|БОНУС|БАЛЛ|ПОДАРОК|БЕЗНАЛ|МОБ\.?\s*ПРИЛОЖ|'
        r'СПАСИБО|ЖДЕМ|ВАС|СНОВА|ДОБРО\s*ПОЖАЛОВАТЬ|'
        r'КОЛ-ВО|ЦЕНА|СТОИМОСТЬ|АРТИКУЛ|АРТ|НАИМЕНОВАНИЕ|'
        r'ИТОГ\s*СО\s*СКИДКОЙ|СТАВКА\s*НДС|БЕЗ\s*СКИДКИ|'
        r'\d{2}[./-]\d{2}[./-]\d{2,4}|'
        r'\d{1,2}:\d{2}:\d{2}|'
        r'ИНН\s*\d+|ОГРН\s*\d+|КПП\s*\d+)',
        re.IGNORECASE)

    def is_service_line(s):
        s_no_space = re.sub(r'\s', '', s)
        if not s_no_space:
            return True
        digits_and_syms = sum(1 for c in s_no_space if c.isdigit() or c in '.,-+*/=№#')
        return digits_and_syms / len(s_no_space) > 0.7

    lines = text.split('\n')
    for line in lines:
        line = line.strip()
        if len(line) < 3: continue
        if stop.search(line): continue
        if is_service_line(line): continue
        line = re.sub(r'^[\d]+[.)]\s*', '', line)
        m = price_line.match(line)
        if not m: continue
        name = m.group(1).strip(' .-*:').strip()
        if len(name) < 2 or len(name) > 80: continue
        if not re.search(r'[А-Яа-яA-Za-z]', name): continue
        if name.isupper() and len(name) < 4: continue
        qty = m.group(2)
        price_str = m.group(3).replace(',', '.')
        total_str = m.group(4)
        try:
            price = float(price_str)
            quantity = float(qty.replace(',', '.')) if qty else 1.0
            if quantity <= 0: quantity = 1.0
            if total_str:
                total = float(total_str.replace(',', '.'))
                if quantity > 1: price = round(total / quantity, 2)
                else: price = total
            elif quantity > 1:
                price = round(price / quantity, 2)
            if price <= 0 or price > 100000: continue
            items.append({'name': name[:150], 'price': round(price, 2), 'quantity': quantity})
        except Exception:
            continue

    unique = []
    seen = set()
    for it in items:
        key = (it['name'].lower(), it['price'], it['quantity'])
        if key in seen: continue
        seen.add(key)
        unique.append(it)

    print(f"📋 Распознано позиций: {len(unique)}")
    for it in unique:
        print(f"   {it['name']} | {it['quantity']} x {it['price']}")
    return unique


def parse_receipt_text_fallback(text):
    if not text: return []
    items = []
    stop = re.compile(
        r'(ИТОГ|ИТОГО|ВСЕГО|СУММА|НАЛИЧНЫМИ|СДАЧА|КАРТОЙ|ПОЛУЧЕНО|'
        r'ВНЕСЕНО|НДС|СКИДКА|КАССИР|СМЕНА|ФД|ФН|ФП|ИНН|ОФД|'
        r'ЭКВ\.?\s*НАЛИЧ|ЭКВ\.?\s*КАРТ|ОПЛАТА|ПЛАТЕЖ|ТЕРМИНАЛ|'
        r'\d{2}[./-]\d{2}[./-]\d{2,4})',
        re.IGNORECASE)
    price_pattern = re.compile(r'(\d{1,5}[.,]\d{2})')
    lines = text.split('\n')
    for line in lines:
        line = line.strip()
        if len(line) < 4: continue
        if stop.search(line): continue
        matches = list(price_pattern.finditer(line))
        if not matches: continue
        last = matches[-1]
        price_str = last.group(1).replace(',', '.')
        try:
            price = float(price_str)
        except Exception:
            continue
        if price <= 0 or price > 100000: continue
        name = line[:last.start()].strip(' .-*:')
        name = re.sub(r'\s*\d+\s*[xхX×*]\s*$', '', name).strip()
        if len(name) < 2 or len(name) > 80: continue
        if not re.search(r'[А-Яа-яA-Za-z]', name): continue
        items.append({'name': name[:150], 'price': round(price, 2), 'quantity': 1.0})
    unique = []
    seen = set()
    for it in items:
        key = (it['name'].lower(), it['price'])
        if key in seen: continue
        seen.add(key)
        unique.append(it)
    print(f"🔄 Fallback парсер: {len(unique)} позиций")
    return unique


def parse_receipt_image(image_path):
    try:
        try:
            img = preprocess_receipt_image(image_path)
        except Exception as e:
            print(f"⚠️ Препроцессинг упал: {e}")
            img = Image.open(image_path).convert('L')
        text = ''
        for psm in [6, 4]:
            try:
                cfg = f'--oem 3 --psm {psm}'
                t = pytesseract.image_to_string(img, lang='rus+eng', config=cfg)
                print(f"── OCR (psm={psm}) ──")
                print(t)
                print("─" * 40)
                if len(t.strip()) > len(text.strip()):
                    text = t
            except Exception as e:
                print(f"⚠️ OCR psm={psm}: {e}")
        items = parse_receipt_text(text)
        return items
    except Exception as e:
        print(f"⚠️ OCR ошибка: {e}")
        return []


# ============ АВТОРИЗАЦИЯ ============
@app.route('/')
def index():
    if 'user_id' in session:
        return render_template('index.html', user_id=session['user_id'])
    return redirect(url_for('login_page'))

@app.route('/login_page')
def login_page(): return render_template('login.html')

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    u = User.query.filter_by(login=data['login']).first()
    if u and u.password == hash_password(data['password']):
        session['user_id'] = u.id
        return jsonify({'success': True, 'user_id': u.id})
    return jsonify({'success': False, 'error': 'Неверный логин или пароль'})

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    if User.query.filter_by(login=data['login']).first():
        return jsonify({'success': False, 'error': 'Логин уже существует'})
    u = User(login=data['login'], email=data['email'], password=hash_password(data['password']))
    db.session.add(u); db.session.commit()
    session['user_id'] = u.id
    return jsonify({'success': True, 'user_id': u.id})

@app.route('/logout')
def logout():
    session.clear(); return redirect(url_for('login_page'))


# ============ ПРИГЛАШЕНИЯ ============
@app.route('/api/trips/<int:trip_id>/invite', methods=['POST'])
def invite_user(trip_id):
    if 'user_id' not in session: return jsonify({'success': False})
    login = request.json.get('login', '').strip()
    if not login: return jsonify({'success': False, 'error': 'Введите логин'})
    trip = Trip.query.get(trip_id)
    if not trip: return jsonify({'success': False, 'error': 'Поездка не найдена'})
    user = User.query.filter_by(login=login).first()
    if not user: return jsonify({'success': False, 'error': 'Пользователь не найден'})
    if user.id == session['user_id']:
        return jsonify({'success': False, 'error': 'Нельзя пригласить себя'})
    if TripParticipant.query.filter_by(trip_id=trip_id, user_id=user.id).first():
        return jsonify({'success': False, 'error': 'Уже участник'})
    existing = Invitation.query.filter_by(trip_id=trip_id, to_user_id=user.id, status='pending').first()
    if existing: return jsonify({'success': False, 'error': 'Приглашение уже отправлено'})
    inv = Invitation(trip_id=trip_id, from_user_id=session['user_id'], to_user_id=user.id, status='pending')
    db.session.add(inv)
    inviter = User.query.get(session['user_id'])
    db.session.add(Notification(user_id=user.id, trip_id=trip_id,
        message=f'✉️ {inviter.login} приглашает вас в поездку "{trip.name}". Нажмите ✉️ в шапке.',
        is_read=False))
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/invitations', methods=['GET'])
def get_my_invitations():
    if 'user_id' not in session: return jsonify([])
    invs = Invitation.query.filter_by(to_user_id=session['user_id'], status='pending').all()
    result = []
    for inv in invs:
        trip = Trip.query.get(inv.trip_id)
        inviter = User.query.get(inv.from_user_id)
        result.append({'id': inv.id, 'trip_id': inv.trip_id,
            'trip_name': trip.name if trip else '?',
            'from_user': inviter.login if inviter else '?',
            'created_at': inv.created_at.strftime('%Y-%m-%d %H:%M')})
    return jsonify(result)

@app.route('/api/invitations/<int:inv_id>/accept', methods=['POST'])
def accept_invitation(inv_id):
    if 'user_id' not in session: return jsonify({'success': False})
    inv = Invitation.query.get(inv_id)
    if not inv or inv.to_user_id != session['user_id']:
        return jsonify({'success': False})
    inv.status = 'accepted'
    db.session.add(TripParticipant(trip_id=inv.trip_id, user_id=inv.to_user_id, role='member'))
    user = User.query.get(inv.to_user_id); trip = Trip.query.get(inv.trip_id)
    db.session.add(Notification(user_id=inv.from_user_id, trip_id=inv.trip_id,
        message=f'✅ {user.login} принял(а) приглашение в "{trip.name}"', is_read=False))
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/invitations/<int:inv_id>/decline', methods=['POST'])
def decline_invitation(inv_id):
    if 'user_id' not in session: return jsonify({'success': False})
    inv = Invitation.query.get(inv_id)
    if not inv or inv.to_user_id != session['user_id']: return jsonify({'success': False})
    inv.status = 'declined'
    user = User.query.get(inv.to_user_id); trip = Trip.query.get(inv.trip_id)
    db.session.add(Notification(user_id=inv.from_user_id, trip_id=inv.trip_id,
        message=f'❌ {user.login} отклонил(а) приглашение в "{trip.name}"', is_read=False))
    db.session.commit()
    return jsonify({'success': True})


# ============ ПУТЕШЕСТВИЯ ============
@app.route('/api/trips', methods=['GET'])
def get_trips():
    if 'user_id' not in session: return jsonify([])
    trips = db.session.query(Trip).join(TripParticipant).filter(
        TripParticipant.user_id == session['user_id']).all()
    return jsonify([{'id': t.id, 'name': t.name, 'destination': t.destination,
        'start_date': t.start_date.strftime('%Y-%m-%d') if t.start_date else None,
        'end_date': t.end_date.strftime('%Y-%m-%d') if t.end_date else None,
        'budget': float(t.budget) if t.budget else 0} for t in trips])

@app.route('/api/trips', methods=['POST'])
def create_trip():
    if 'user_id' not in session: return jsonify({'success': False})
    return jsonify(CreateTripCommand(request.json, session['user_id']).execute())

@app.route('/api/trips/<int:trip_id>', methods=['DELETE'])
def delete_trip(trip_id):
    if 'user_id' not in session: return jsonify({'success': False})
    t = Trip.query.get(trip_id)
    if t and t.created_by == session['user_id']:
        receipts = Receipt.query.filter_by(trip_id=trip_id).all()
        for r in receipts:
            ReceiptItem.query.filter_by(receipt_id=r.id).delete()
        Receipt.query.filter_by(trip_id=trip_id).delete()
        Notification.query.filter_by(trip_id=trip_id).delete()
        Invitation.query.filter_by(trip_id=trip_id).delete()
        Debt.query.filter_by(trip_id=trip_id).delete()
        Expense.query.filter_by(trip_id=trip_id).delete()
        Task.query.filter_by(trip_id=trip_id).delete()
        Checklist.query.filter_by(trip_id=trip_id).delete()
        Place.query.filter_by(trip_id=trip_id).delete()
        TripParticipant.query.filter_by(trip_id=trip_id).delete()
        db.session.delete(t); db.session.commit()
        return jsonify({'success': True})
    return jsonify({'success': False})


# ============ БЮДЖЕТ ============
@app.route('/api/trips/<int:trip_id>/budget', methods=['GET'])
def get_budget(trip_id):
    trip = Trip.query.get(trip_id)
    if not trip: return jsonify({'error': 'not found'})
    total_budget = float(trip.budget) if trip.budget else 0
    spent = sum(float(e.amount) for e in Expense.query.filter_by(trip_id=trip_id).all())
    return jsonify({'total_budget': round(total_budget, 2), 'spent': round(spent, 2),
                    'remaining': round(total_budget - spent, 2)})


# ============ УЧАСТНИКИ ============
@app.route('/api/trips/<int:trip_id>/participants', methods=['GET'])
def get_participants(trip_id):
    if 'user_id' not in session: return jsonify([])
    if not TripParticipant.query.filter_by(trip_id=trip_id, user_id=session['user_id']).first():
        return jsonify([])
    rows = db.session.query(User, TripParticipant.role).join(
        TripParticipant, User.id == TripParticipant.user_id
    ).filter(TripParticipant.trip_id == trip_id).all()
    return jsonify([{'id': u.id, 'login': u.login, 'role': role} for u, role in rows])


# ============ РАСХОДЫ ============
@app.route('/api/trips/<int:trip_id>/expenses', methods=['POST'])
def add_expense(trip_id):
    if 'user_id' not in session: return jsonify({'success': False})
    res = AddExpenseCommand(trip_id, request.json, session['user_id']).execute()
    calculate_debts(trip_id)
    return jsonify(res)

@app.route('/api/trips/<int:trip_id>/expenses', methods=['GET'])
def get_expenses(trip_id):
    expenses = Expense.query.filter_by(trip_id=trip_id).order_by(Expense.date.desc()).all()
    uids = {e.paid_by for e in expenses}
    users = User.query.filter(User.id.in_(uids)).all() if uids else []
    um = {u.id: u.login for u in users}
    total = 0; result = []
    for e in expenses:
        total += e.amount
        result.append({'id': e.id, 'amount': float(e.amount), 'description': e.description,
            'paid_by': um.get(e.paid_by, 'Неизвестный'),
            'date': e.date.strftime('%Y-%m-%d'),
            'receipt_id': e.receipt_id})
    return jsonify({'expenses': result, 'total_spent': round(total, 2)})


# ============ ДОЛГИ ============
def calculate_debts(trip_id):
    participants = TripParticipant.query.filter_by(trip_id=trip_id).all()
    if not participants: return
    uids = [p.user_id for p in participants]
    balance = {uid: 0.0 for uid in uids}

    for e in Expense.query.filter_by(trip_id=trip_id).all():
        if e.receipt_id:
            continue
        per = e.amount / len(uids)
        balance[e.paid_by] += e.amount
        for uid in uids: balance[uid] -= per

    receipts = Receipt.query.filter_by(trip_id=trip_id).all()
    for r in receipts:
        items = ReceiptItem.query.filter_by(receipt_id=r.id).all()
        for item in items:
            if item.shared_by:
                shared_ids = [int(x) for x in item.shared_by.split(',') if x.strip().isdigit()]
            else:
                shared_ids = uids
            if not shared_ids: continue
            per = (item.price * item.quantity) / len(shared_ids)
            balance[r.uploaded_by] += item.price * item.quantity
            for uid in shared_ids:
                balance[uid] -= per

    Debt.query.filter_by(trip_id=trip_id).delete()
    crs = [(u, b) for u, b in balance.items() if b > 0]
    dbs = [(u, -b) for u, b in balance.items() if b < 0]
    i = j = 0
    while i < len(crs) and j < len(dbs):
        cid, camt = crs[i]; did, damt = dbs[j]
        amt = min(camt, damt)
        db.session.add(Debt(trip_id=trip_id, from_user_id=did, to_user_id=cid,
                            amount=round(amt, 2), is_settled=False))
        crs[i] = (cid, camt - amt); dbs[j] = (did, damt - amt)
        if crs[i][1] <= 0.01: i += 1
        if dbs[j][1] <= 0.01: j += 1
    db.session.commit()

@app.route('/api/trips/<int:trip_id>/debts', methods=['GET'])
def get_debts(trip_id):
    debts = Debt.query.filter_by(trip_id=trip_id, is_settled=False).all()
    uids = set()
    for d in debts: uids.add(d.from_user_id); uids.add(d.to_user_id)
    users = User.query.filter(User.id.in_(uids)).all() if uids else []
    um = {u.id: u.login for u in users}
    return jsonify([{'from_user': um.get(d.from_user_id, '?'),
        'to_user': um.get(d.to_user_id, '?'), 'amount': float(d.amount)} for d in debts])

@app.route('/api/trips/<int:trip_id>/calculate', methods=['POST'])
def recalculate_debts(trip_id):
    calculate_debts(trip_id)
    return jsonify({'success': True})


# ============ ЧЕКИ ============
@app.route('/api/trips/<int:trip_id>/receipts/upload', methods=['POST'])
def upload_receipt(trip_id):
    if 'user_id' not in session: return jsonify({'success': False})
    if 'image' not in request.files:
        return jsonify({'success': False, 'error': 'Файл не передан'})
    file = request.files['image']
    if not file.filename:
        return jsonify({'success': False, 'error': 'Пустое имя файла'})

    ext = os.path.splitext(file.filename)[1].lower() or '.jpg'
    filename = f"receipt_{trip_id}_{int(datetime.now().timestamp())}{ext}"
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    items = parse_receipt_image(filepath)

    if not items:
        print("⚠️ Основной парсер не дал результата, пробуем fallback...")
        try:
            _img = Image.open(filepath).convert('L')
            _text = pytesseract.image_to_string(_img, lang='rus+eng', config='--oem 3 --psm 6')
            items = parse_receipt_text_fallback(_text)
        except Exception as e:
            print(f"⚠️ Fallback OCR ошибка: {e}")

    total_amount = sum(it['price'] * it['quantity'] for it in items)

    r = Receipt(trip_id=trip_id, uploaded_by=session['user_id'],
                image_filename=filename, total_amount=round(total_amount, 2),
                status='pending')
    db.session.add(r); db.session.flush()

    for it in items:
        ri = ReceiptItem(receipt_id=r.id, name=it['name'],
                         price=it['price'], quantity=it['quantity'], shared_by='')
        db.session.add(ri)

    db.session.commit()
    return jsonify({'success': True, 'receipt_id': r.id, 'items_count': len(items),
                    'total_amount': round(total_amount, 2),
                    'image_url': f'/static/receipts/{filename}'})

@app.route('/api/receipts/<int:receipt_id>', methods=['GET'])
def get_receipt(receipt_id):
    if 'user_id' not in session: return jsonify({})
    r = Receipt.query.get(receipt_id)
    if not r: return jsonify({})
    items = ReceiptItem.query.filter_by(receipt_id=receipt_id).all()
    uploader = User.query.get(r.uploaded_by)
    return jsonify({
        'id': r.id, 'trip_id': r.trip_id, 'status': r.status,
        'total_amount': float(r.total_amount) if r.total_amount else 0,
        'image_url': f'/static/receipts/{r.image_filename}' if r.image_filename else None,
        'uploaded_by': uploader.login if uploader else '?',
        'items': [{'id': it.id, 'name': it.name, 'price': float(it.price),
                   'quantity': float(it.quantity), 'shared_by': it.shared_by} for it in items]
    })

@app.route('/api/receipts/<int:receipt_id>/items', methods=['PUT'])
def update_receipt_items(receipt_id):
    if 'user_id' not in session: return jsonify({'success': False})
    r = Receipt.query.get(receipt_id)
    if not r: return jsonify({'success': False, 'error': 'Чек не найден'})

    data = request.json
    items_data = data.get('items', [])
    existing_ids = set()

    for it_data in items_data:
        item_id = it_data['id']
        if item_id and item_id > 0:
            item = ReceiptItem.query.get(item_id)
            if item and item.receipt_id == receipt_id:
                existing_ids.add(item.id)
                if 'name' in it_data: item.name = it_data['name']
                if 'price' in it_data: item.price = float(it_data['price'])
                if 'quantity' in it_data: item.quantity = float(it_data['quantity'])
                if 'shared_by' in it_data:
                    if isinstance(it_data['shared_by'], list):
                        item.shared_by = ','.join(str(x) for x in it_data['shared_by'])
                    else:
                        item.shared_by = str(it_data['shared_by'])
        else:
            sb = it_data.get('shared_by', '')
            if isinstance(sb, list):
                sb = ','.join(str(x) for x in sb)
            new_item = ReceiptItem(
                receipt_id=receipt_id,
                name=it_data.get('name', 'Позиция'),
                price=float(it_data.get('price', 0)),
                quantity=float(it_data.get('quantity', 1)),
                shared_by=sb)
            db.session.add(new_item)
            db.session.flush()
            existing_ids.add(new_item.id)

    all_items = ReceiptItem.query.filter_by(receipt_id=receipt_id).all()
    for it in all_items:
        if it.id not in existing_ids:
            db.session.delete(it)

    db.session.flush()
    remaining = ReceiptItem.query.filter_by(receipt_id=receipt_id).all()
    r.total_amount = sum(it.price * it.quantity for it in remaining)
    db.session.commit()
    calculate_debts(r.trip_id)
    return jsonify({'success': True, 'total_amount': round(r.total_amount, 2)})

@app.route('/api/receipts/<int:receipt_id>/confirm', methods=['POST'])
def confirm_receipt(receipt_id):
    if 'user_id' not in session: return jsonify({'success': False})
    r = Receipt.query.get(receipt_id)
    if not r: return jsonify({'success': False})
    r.status = 'confirmed'
    items = ReceiptItem.query.filter_by(receipt_id=r.id).all()
    total = sum(it.price * it.quantity for it in items)
    Expense.query.filter_by(receipt_id=r.id).delete()
    e = Expense(trip_id=r.trip_id, amount=total,
                description=f'🧾 Чек №{r.id}',
                paid_by=r.uploaded_by,
                date=datetime.now().date(),
                receipt_id=r.id)
    db.session.add(e)
    db.session.commit()
    calculate_debts(r.trip_id)
    return jsonify({'success': True})

@app.route('/api/trips/<int:trip_id>/receipts', methods=['GET'])
def get_trip_receipts(trip_id):
    if 'user_id' not in session: return jsonify([])
    receipts = Receipt.query.filter_by(trip_id=trip_id).order_by(Receipt.created_at.desc()).all()
    result = []
    for r in receipts:
        uploader = User.query.get(r.uploaded_by)
        items_count = ReceiptItem.query.filter_by(receipt_id=r.id).count()
        result.append({
            'id': r.id, 'total_amount': float(r.total_amount) if r.total_amount else 0,
            'status': r.status, 'items_count': items_count,
            'uploaded_by': uploader.login if uploader else '?',
            'image_url': f'/static/receipts/{r.image_filename}' if r.image_filename else None,
            'created_at': r.created_at.strftime('%Y-%m-%d %H:%M')
        })
    return jsonify(result)

@app.route('/api/receipts/<int:receipt_id>', methods=['DELETE'])
def delete_receipt(receipt_id):
    if 'user_id' not in session: return jsonify({'success': False})
    r = Receipt.query.get(receipt_id)
    if not r: return jsonify({'success': False})
    trip_id = r.trip_id
    Expense.query.filter_by(receipt_id=receipt_id).delete()
    ReceiptItem.query.filter_by(receipt_id=receipt_id).delete()
    if r.image_filename:
        fpath = os.path.join(app.config['UPLOAD_FOLDER'], r.image_filename)
        if os.path.exists(fpath):
            try: os.remove(fpath)
            except: pass
    db.session.delete(r); db.session.commit()
    calculate_debts(trip_id)
    return jsonify({'success': True})


# ============ ЗАДАЧИ ============
@app.route('/api/trips/<int:trip_id>/tasks', methods=['POST'])
def create_task(trip_id):
    if 'user_id' not in session: return jsonify({'success': False})
    data = request.json
    t = Task(trip_id=trip_id, title=data['title'],
        deadline=datetime.strptime(data['deadline'], '%Y-%m-%d').date() if data.get('deadline') else None,
        assigned_to=int(data['assigned_to']) if data.get('assigned_to') else None,
        is_completed=False)
    db.session.add(t); db.session.commit()
    if t.deadline:
        event_manager.notify('task_created', {'trip_id': trip_id, 'task_title': t.title,
                                              'deadline': t.deadline.strftime('%Y-%m-%d')})
    return jsonify({'id': t.id, 'success': True})

@app.route('/api/trips/<int:trip_id>/tasks', methods=['GET'])
def get_tasks(trip_id):
    tasks = Task.query.filter_by(trip_id=trip_id).order_by(Task.deadline).all()
    return jsonify([{'id': t.id, 'title': t.title,
        'deadline': t.deadline.strftime('%Y-%m-%d') if t.deadline else None,
        'assigned_to': t.assigned_to, 'is_completed': t.is_completed} for t in tasks])

@app.route('/api/tasks/<int:task_id>/complete', methods=['PUT'])
def complete_task(task_id):
    return jsonify(CompleteTaskCommand(task_id).execute())


# ============ ЧЕК-ЛИСТ ============
@app.route('/api/trips/<int:trip_id>/checklist', methods=['POST'])
def add_checklist_item(trip_id):
    if 'user_id' not in session: return jsonify({'success': False})
    data = request.json
    i = Checklist(trip_id=trip_id, item_name=data['item_name'],
        assigned_to=int(data['assigned_to']) if data.get('assigned_to') else None,
        is_checked=False)
    db.session.add(i); db.session.commit()
    return jsonify({'id': i.id, 'success': True})

@app.route('/api/trips/<int:trip_id>/checklist', methods=['GET'])
def get_checklist(trip_id):
    items = Checklist.query.filter_by(trip_id=trip_id).all()
    assigned_ids = {i.assigned_to for i in items if i.assigned_to}
    users = User.query.filter(User.id.in_(assigned_ids)).all() if assigned_ids else []
    user_map = {u.id: u.login for u in users}
    return jsonify([{'id': i.id, 'item_name': i.item_name,
        'assigned_to': i.assigned_to,
        'assigned_to_name': user_map.get(i.assigned_to, 'не назначен'),
        'is_checked': i.is_checked} for i in items])

@app.route('/api/checklist/<int:item_id>/check', methods=['PUT'])
def check_item(item_id):
    i = Checklist.query.get(item_id)
    if i:
        i.is_checked = True; db.session.commit()
        return jsonify({'success': True})
    return jsonify({'success': False})


# ============ МЕСТА ============
@app.route('/api/trips/<int:trip_id>/places', methods=['POST'])
def add_place(trip_id):
    if 'user_id' not in session: return jsonify({'success': False})
    data = request.json
    parsed = {}
    if data.get('source_url'): parsed = parse_place_link(data['source_url'])
    name = (data.get('name') or parsed.get('name') or 'Место по ссылке').strip()
    address = (data.get('address') or parsed.get('address') or '').strip()
    notes = data.get('notes') or parsed.get('notes') or ''
    image_url = parsed.get('image_url') or data.get('image_url')
    if not image_url:
        wiki_img = fetch_wikipedia_image(name)
        if wiki_img: image_url = wiki_img
    if not image_url:
        image_url = 'https://via.placeholder.com/400x200/667eea/ffffff?text=Place'
    trip = Trip.query.get(trip_id)
    city_hint = trip.destination if trip else None
    lat = data.get('latitude'); lng = data.get('longitude')
    if not lat or not lng:
        lat, lng = geocode_address(name=name, address=address, city_hint=city_hint)
    place = Place(trip_id=trip_id, name=name, address=address,
        image_url=image_url, source_url=data.get('source_url'), notes=notes,
        visit_date=datetime.strptime(data['visit_date'], '%Y-%m-%d').date() if data.get('visit_date') else None,
        visit_time=datetime.strptime(data['visit_time'], '%H:%M').time() if data.get('visit_time') else None,
        latitude=lat, longitude=lng)
    db.session.add(place); db.session.commit()
    return jsonify({'id': place.id, 'success': True, 'latitude': lat, 'longitude': lng})

@app.route('/api/trips/<int:trip_id>/places', methods=['GET'])
def get_places(trip_id):
    places = Place.query.filter_by(trip_id=trip_id).order_by(Place.visit_date, Place.visit_time).all()
    return jsonify([{'id': p.id, 'name': p.name, 'address': p.address,
        'image_url': p.image_url, 'source_url': p.source_url, 'notes': p.notes,
        'visit_date': p.visit_date.strftime('%Y-%m-%d') if p.visit_date else None,
        'visit_time': p.visit_time.strftime('%H:%M') if p.visit_time else None,
        'latitude': p.latitude, 'longitude': p.longitude} for p in places])

@app.route('/api/places/<int:place_id>', methods=['DELETE'])
def delete_place(place_id):
    p = Place.query.get(place_id)
    if p:
        db.session.delete(p); db.session.commit()
        return jsonify({'success': True})
    return jsonify({'success': False})

@app.route('/api/places/<int:place_id>/set-coords', methods=['PUT'])
def set_place_coords(place_id):
    if 'user_id' not in session: return jsonify({'success': False})
    data = request.json
    p = Place.query.get(place_id)
    if not p: return jsonify({'success': False})
    p.latitude = data.get('latitude'); p.longitude = data.get('longitude')
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/places/<int:place_id>/set-image', methods=['PUT'])
def set_place_image(place_id):
    if 'user_id' not in session: return jsonify({'success': False})
    data = request.json
    p = Place.query.get(place_id)
    if not p: return jsonify({'success': False})
    p.image_url = data.get('image_url')
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/places/<int:place_id>/stops', methods=['GET'])
def get_place_stops(place_id):
    if 'user_id' not in session: return jsonify([])
    p = Place.query.get(place_id)
    if not p or not p.latitude or not p.longitude: return jsonify([])
    return jsonify(find_nearby_stops(p.latitude, p.longitude, radius_m=800, limit=6))


# ============ СТАРТОВАЯ ТОЧКА МАРШРУТА ============
@app.route('/api/trips/<int:trip_id>/route-start', methods=['POST'])
def set_route_start(trip_id):
    if 'user_id' not in session: return jsonify({'success': False})
    data = request.json
    trip = Trip.query.get(trip_id)
    if not trip: return jsonify({'success': False})
    address = data.get('address', '').strip()
    lat = data.get('latitude'); lng = data.get('longitude')
    if (not lat or not lng) and address:
        lat, lng = geocode_address(address=address)
    trip.route_start_address = address or None
    trip.route_start_lat = lat; trip.route_start_lng = lng
    db.session.commit()
    if lat and lng:
        return jsonify({'success': True, 'latitude': lat, 'longitude': lng})
    return jsonify({'success': False, 'error': 'Не удалось найти координаты старта'})

@app.route('/api/trips/<int:trip_id>/route-start', methods=['GET'])
def get_route_start(trip_id):
    trip = Trip.query.get(trip_id)
    if not trip: return jsonify({})
    return jsonify({'address': trip.route_start_address,
        'latitude': trip.route_start_lat, 'longitude': trip.route_start_lng})


# ============ МАРШРУТ ЧЕРЕЗ OSRM ============
@app.route('/api/trips/<int:trip_id>/route', methods=['GET'])
def get_route(trip_id):
    trip = Trip.query.get(trip_id)
    if not trip: return jsonify({'success': False})
    include_start = request.args.get('include_start', '1') != '0'
    place_ids_param = request.args.get('place_ids', '').strip()
    query = Place.query.filter_by(trip_id=trip_id)
    if place_ids_param:
        ids = [int(x) for x in place_ids_param.split(',') if x.strip().isdigit()]
        if ids: query = query.filter(Place.id.in_(ids))
    places = query.all()
    points = []
    if include_start and trip.route_start_lat and trip.route_start_lng:
        points.append((trip.route_start_lng, trip.route_start_lat))
    for p in places:
        if p.latitude and p.longitude:
            points.append((p.longitude, p.latitude))
    if len(points) < 2:
        return jsonify({'success': False, 'error': 'Нужно минимум 2 точки'})
    coords = ';'.join([f'{lng},{lat}' for lng, lat in points])
    url = f'http://router.project-osrm.org/route/v1/driving/{coords}?overview=full&geometries=geojson'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'PoehaliVmeste/1.0'})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode('utf-8'))
        if data.get('code') != 'Ok' or not data.get('routes'):
            return jsonify({'success': False, 'error': 'OSRM не смог построить маршрут'})
        route = data['routes'][0]
        latlngs = [[c[1], c[0]] for c in route['geometry']['coordinates']]
        return jsonify({'success': True, 'polyline': latlngs, 'points_count': len(points)})
    except Exception as e:
        print(f"⚠️ OSRM ошибка: {e}")
        return jsonify({'success': False, 'error': str(e)})


# ============ УВЕДОМЛЕНИЯ ============
@app.route('/api/notifications', methods=['GET'])
def get_notifications():
    if 'user_id' not in session: return jsonify([])
    ns = Notification.query.filter_by(user_id=session['user_id'], is_read=False)\
        .order_by(Notification.created_at.desc()).all()
    return jsonify([{'id': n.id, 'message': n.message, 'trip_id': n.trip_id,
        'created_at': n.created_at.strftime('%Y-%m-%d %H:%M')} for n in ns])

@app.route('/api/notifications/read-all', methods=['PUT'])
def mark_all_notifications_read():
    if 'user_id' not in session: return jsonify({'success': False})
    Notification.query.filter_by(user_id=session['user_id'], is_read=False).update({'is_read': True})
    db.session.commit()
    return jsonify({'success': True})


if __name__ == '__main__':
    d1 = DatabaseManager(); d2 = DatabaseManager()
    if d1 is d2: print("✅ Singleton работает")
    app.run(debug=True)