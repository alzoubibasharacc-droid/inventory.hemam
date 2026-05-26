from models import db, Branch, Department, Unit, UnitConversion, Category, User


def seed_data():
    if Branch.query.first():
        return  # Already seeded

    # --- Branches & Departments ---
    branches_data = [
        {
            'name_ar': 'فرع جراند', 'name_en': 'Grand Branch',
            'depts': [
                ('المخبز', 'Bakery'),
                ('الباريستا', 'Barista'),
                ('المستودع', 'Warehouse'),
                ('المطبخ', 'Kitchen'),
            ]
        },
        {
            'name_ar': 'الفرع الرئيسي', 'name_en': 'Main Branch',
            'depts': [
                ('المستودع', 'Warehouse'),
                ('المطبخ', 'Kitchen'),
            ]
        },
        {
            'name_ar': 'فرع الهاشمي', 'name_en': 'Hashemi Branch',
            'depts': [
                ('المستودع', 'Warehouse'),
                ('المطبخ', 'Kitchen'),
            ]
        },
    ]

    for b_data in branches_data:
        branch = Branch(name_ar=b_data['name_ar'], name_en=b_data['name_en'])
        db.session.add(branch)
        db.session.flush()
        for d_ar, d_en in b_data['depts']:
            dept = Department(name_ar=d_ar, name_en=d_en, branch_id=branch.id)
            db.session.add(dept)

    # --- Units ---
    # (name_ar, name_en, symbol)
    units_data = [
        ('كيلوغرام', 'Kilogram',   'kg'),
        ('غرام',     'Gram',       'g'),
        ('لتر',      'Liter',      'L'),
        ('مليلتر',   'Milliliter', 'mL'),
        ('قطعة',     'Piece',      'pcs'),
        ('علبة',     'Box',        'box'),
        ('كيس',      'Bag',        'bag'),
        ('كرتون',    'Carton',     'ctn'),
        ('زجاجة',   'Bottle',     'btl'),
    ]
    unit_objs = {}
    for name_ar, name_en, symbol in units_data:
        u = Unit(name_ar=name_ar, name_en=name_en, symbol=symbol, is_active=True)
        db.session.add(u)
        db.session.flush()
        unit_objs[symbol] = u

    # Unit conversions
    conversions = [
        ('kg', 'g', 1000),
        ('g', 'kg', 0.001),
        ('L', 'mL', 1000),
        ('mL', 'L', 0.001),
    ]
    for from_en, to_en, factor in conversions:
        conv = UnitConversion(
            from_unit_id=unit_objs[from_en].id,
            to_unit_id=unit_objs[to_en].id,
            factor=factor
        )
        db.session.add(conv)

    # --- Categories ---
    categories_data = [
        ('مواد غذائية جافة', 'Dry Goods'),
        ('ألبان وأجبان', 'Dairy'),
        ('لحوم ودواجن', 'Meat & Poultry'),
        ('خضروات وفواكه', 'Vegetables & Fruits'),
        ('مشروبات', 'Beverages'),
        ('توابل وبهارات', 'Spices'),
        ('مستلزمات التعبئة', 'Packaging'),
    ]
    for c_ar, c_en in categories_data:
        db.session.add(Category(name_ar=c_ar, name_en=c_en))

    # --- Default Admin User ---
    admin = User(
        username='admin',
        full_name='المدير العام',
        role='admin'
    )
    admin.set_password('admin123')
    db.session.add(admin)

    db.session.commit()
    print("تم تهيئة قاعدة البيانات بنجاح")
    print("المستخدم الافتراضي: admin / admin123")
