from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
import sqlite3
import os
import json
from openai import OpenAI

app = Flask(__name__, static_folder='.')
DB_PATH = os.path.join(os.path.dirname(__file__), 'pat_s.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS patients (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            diagnosis   TEXT    DEFAULT '',
            created_at  TEXT    DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS measurements (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id  INTEGER NOT NULL,
            date        TEXT    NOT NULL,
            sane        INTEGER,
            pain_nrs    INTEGER,
            gpc         INTEGER,
            notes       TEXT    DEFAULT '',
            created_at  TEXT    DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (patient_id) REFERENCES patients(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS psfs_scores (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            measurement_id  INTEGER NOT NULL,
            activity_name   TEXT    NOT NULL,
            score           INTEGER NOT NULL,
            FOREIGN KEY (measurement_id) REFERENCES measurements(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS function_tests (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            measurement_id  INTEGER NOT NULL,
            test_name       TEXT    NOT NULL,
            test_value      REAL,
            test_unit       TEXT    DEFAULT '',
            FOREIGN KEY (measurement_id) REFERENCES measurements(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS therapists (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            first_name TEXT NOT NULL,
            last_name  TEXT NOT NULL,
            signature  TEXT DEFAULT ''
        );
    ''')
    conn.commit()

    # Migration: icd_code in patients
    try:
        conn.execute("ALTER TABLE patients ADD COLUMN icd_code TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass

    # Migration: therapist_id in measurements
    try:
        conn.execute('ALTER TABLE measurements ADD COLUMN therapist_id INTEGER REFERENCES therapists(id)')
        conn.commit()
    except Exception:
        pass

    # Pre-populate therapists if table is empty
    if conn.execute('SELECT COUNT(*) FROM therapists').fetchone()[0] == 0:
        therapists_data = [
            ('Theresa', 'Bode'), ('Luisa', 'Goldbach'), ('Elisabeth', 'Driediger'),
            ('Anna', 'Gorlt'), ('Mirko', 'Koster'), ('Vanessa', 'Koster'),
            ('Jennifer', 'Tann'), ('Laura', 'Dey'), ('Michal', 'Kara'),
            ('Marc-Fynn', 'Michael'), ('Sven', 'Schäfer'), ('Irene', 'Scheubel'),
            ('Hanna', 'Taubert'), ('Anna', 'Lehn'), ('Hristina', 'Nechovska'),
            ('Katharina', 'Heubaum'), ('Praktikant', 'Schule'),
        ]
        conn.executemany('INSERT INTO therapists (first_name, last_name) VALUES (?, ?)', therapists_data)
        conn.commit()

    conn.close()


# ── Patienten ──────────────────────────────────────────────────────────────────

@app.route('/api/patients', methods=['GET', 'POST'])
def handle_patients():
    conn = get_db()
    try:
        if request.method == 'GET':
            rows = conn.execute(
                'SELECT * FROM patients ORDER BY name COLLATE NOCASE'
            ).fetchall()
            return jsonify([dict(r) for r in rows])

        data = request.get_json()
        name = (data.get('name') or '').strip()
        if not name:
            return jsonify({'error': 'Name erforderlich'}), 400
        conn.execute(
            'INSERT INTO patients (name, diagnosis, icd_code) VALUES (?, ?, ?)',
            (name, data.get('diagnosis', '').strip(), data.get('icd_code', '').strip().upper())
        )
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()


@app.route('/api/patients/<int:pid>', methods=['DELETE'])
def delete_patient(pid):
    conn = get_db()
    try:
        conn.execute('DELETE FROM patients WHERE id = ?', (pid,))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()


# ── Messungen ──────────────────────────────────────────────────────────────────

@app.route('/api/measurements', methods=['GET', 'POST'])
def handle_measurements():
    conn = get_db()
    try:
        if request.method == 'GET':
            pid = request.args.get('patient_id')
            if not pid:
                return jsonify({'error': 'patient_id required'}), 400

            rows = conn.execute(
                '''SELECT m.*, COALESCE(t.first_name || ' ' || t.last_name, '') AS therapist_name
                   FROM measurements m
                   LEFT JOIN therapists t ON t.id = m.therapist_id
                   WHERE m.patient_id = ? ORDER BY m.date ASC''',
                (pid,)
            ).fetchall()

            result = []
            for row in rows:
                m = dict(row)
                m['psfs'] = [dict(r) for r in conn.execute(
                    'SELECT * FROM psfs_scores WHERE measurement_id = ?', (m['id'],)
                ).fetchall()]
                m['function_tests'] = [dict(r) for r in conn.execute(
                    'SELECT * FROM function_tests WHERE measurement_id = ?', (m['id'],)
                ).fetchall()]
                result.append(m)
            return jsonify(result)

        data = request.get_json()
        if not data.get('patient_id') or not data.get('date'):
            return jsonify({'error': 'patient_id und date erforderlich'}), 400

        cur = conn.execute(
            'INSERT INTO measurements (patient_id, date, sane, pain_nrs, gpc, notes, therapist_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (data['patient_id'], data['date'],
             data.get('sane'), data.get('pain_nrs'), data.get('gpc'),
             data.get('notes', ''), data.get('therapist_id'))
        )
        mid = cur.lastrowid

        for p in (data.get('psfs') or []):
            if p.get('activity_name') and p.get('score') is not None:
                conn.execute(
                    'INSERT INTO psfs_scores (measurement_id, activity_name, score) VALUES (?, ?, ?)',
                    (mid, p['activity_name'], int(p['score']))
                )

        for t in (data.get('function_tests') or []):
            if t.get('test_name') and t.get('test_value') is not None:
                conn.execute(
                    'INSERT INTO function_tests (measurement_id, test_name, test_value, test_unit) VALUES (?, ?, ?, ?)',
                    (mid, t['test_name'], float(t['test_value']), t.get('test_unit', ''))
                )

        conn.commit()
        return jsonify({'success': True, 'id': mid})
    finally:
        conn.close()


@app.route('/api/measurements/<int:mid>', methods=['DELETE', 'PUT'])
def handle_measurement(mid):
    if request.method == 'DELETE':
        conn = get_db()
        try:
            conn.execute('DELETE FROM measurements WHERE id = ?', (mid,))
            conn.commit()
            return jsonify({'success': True})
        finally:
            conn.close()

    # PUT – update existing measurement
    data = request.get_json()
    conn = get_db()
    try:
        conn.execute(
            'UPDATE measurements SET date=?, sane=?, pain_nrs=?, gpc=?, notes=?, therapist_id=? WHERE id=?',
            (data['date'], data.get('sane'), data.get('pain_nrs'), data.get('gpc'),
             data.get('notes', ''), data.get('therapist_id'), mid)
        )
        conn.execute('DELETE FROM psfs_scores WHERE measurement_id=?', (mid,))
        for p in data.get('psfs', []):
            conn.execute(
                'INSERT INTO psfs_scores (measurement_id, activity_name, score) VALUES (?,?,?)',
                (mid, p['activity_name'], p['score'])
            )
        conn.execute('DELETE FROM function_tests WHERE measurement_id=?', (mid,))
        for t in data.get('function_tests', []):
            conn.execute(
                'INSERT INTO function_tests (measurement_id, test_name, test_value, test_unit) VALUES (?,?,?,?)',
                (mid, t['test_name'], t['test_value'], t['test_unit'])
            )
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()


# ── Therapeuten ────────────────────────────────────────────────────────────────

@app.route('/api/therapists', methods=['GET', 'POST'])
def handle_therapists():
    conn = get_db()
    try:
        if request.method == 'GET':
            rows = conn.execute(
                'SELECT id, first_name, last_name, signature FROM therapists ORDER BY last_name, first_name'
            ).fetchall()
            return jsonify([dict(r) for r in rows])

        data = request.get_json()
        first = (data.get('first_name') or '').strip()
        last  = (data.get('last_name')  or '').strip()
        if not first or not last:
            return jsonify({'error': 'Vor- und Nachname erforderlich'}), 400
        conn.execute('INSERT INTO therapists (first_name, last_name) VALUES (?, ?)', (first, last))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()


@app.route('/api/therapists/<int:tid>', methods=['DELETE'])
def delete_therapist(tid):
    conn = get_db()
    try:
        conn.execute('DELETE FROM therapists WHERE id = ?', (tid,))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()


@app.route('/api/therapists/<int:tid>/signature', methods=['POST'])
def upload_signature(tid):
    conn = get_db()
    try:
        data = request.get_json()
        conn.execute('UPDATE therapists SET signature = ? WHERE id = ?', (data.get('signature', ''), tid))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()


# ── Export ─────────────────────────────────────────────────────────────────────

@app.route('/api/export/<int:pid>')
def export_data(pid):
    conn = get_db()
    try:
        patient = conn.execute('SELECT * FROM patients WHERE id = ?', (pid,)).fetchone()
        if not patient:
            return jsonify({'error': 'Patient nicht gefunden'}), 404

        rows = conn.execute(
            'SELECT * FROM measurements WHERE patient_id = ? ORDER BY date', (pid,)
        ).fetchall()

        measurements = []
        for row in rows:
            m = dict(row)
            m['psfs'] = [dict(r) for r in conn.execute(
                'SELECT * FROM psfs_scores WHERE measurement_id = ?', (m['id'],)
            ).fetchall()]
            m['function_tests'] = [dict(r) for r in conn.execute(
                'SELECT * FROM function_tests WHERE measurement_id = ?', (m['id'],)
            ).fetchall()]
            measurements.append(m)

        return jsonify({'patient': dict(patient), 'measurements': measurements})
    finally:
        conn.close()


@app.route('/api/export-all')
def export_all():
    """Exportiert die gesamte Datenbank (alle Patienten + Therapeuten)."""
    conn = get_db()
    try:
        patients = [dict(r) for r in conn.execute('SELECT * FROM patients ORDER BY name COLLATE NOCASE').fetchall()]
        for p in patients:
            rows = conn.execute('SELECT * FROM measurements WHERE patient_id = ? ORDER BY date', (p['id'],)).fetchall()
            measurements = []
            for row in rows:
                m = dict(row)
                m['psfs'] = [dict(r) for r in conn.execute(
                    'SELECT * FROM psfs_scores WHERE measurement_id = ?', (m['id'],)).fetchall()]
                m['function_tests'] = [dict(r) for r in conn.execute(
                    'SELECT * FROM function_tests WHERE measurement_id = ?', (m['id'],)).fetchall()]
                measurements.append(m)
            p['measurements'] = measurements

        therapists = [dict(r) for r in conn.execute('SELECT * FROM therapists ORDER BY last_name, first_name').fetchall()]

        return jsonify({
            'version': 1,
            'exported_at': __import__('datetime').datetime.now().isoformat(),
            'patients': patients,
            'therapists': therapists,
        })
    finally:
        conn.close()


@app.route('/api/import-all', methods=['POST'])
def import_all():
    """Importiert einen Vollexport. Patienten/Messungen werden anhand von Name+Datum zusammengeführt."""
    data = request.get_json()
    if not data or 'patients' not in data:
        return jsonify({'error': 'Ungültiges Format'}), 400

    conn = get_db()
    stats = {'patients_new': 0, 'patients_existing': 0, 'measurements_new': 0, 'therapists_new': 0}
    try:
        # Therapeuten importieren (nur neue anlegen)
        for t in (data.get('therapists') or []):
            exists = conn.execute(
                'SELECT id FROM therapists WHERE first_name=? AND last_name=?',
                (t['first_name'], t['last_name'])
            ).fetchone()
            if not exists:
                conn.execute(
                    'INSERT INTO therapists (first_name, last_name, signature) VALUES (?,?,?)',
                    (t['first_name'], t['last_name'], t.get('signature', ''))
                )
                stats['therapists_new'] += 1

        # Patienten + Messungen importieren
        for p in data['patients']:
            existing = conn.execute(
                'SELECT id FROM patients WHERE name=?', (p['name'],)
            ).fetchone()
            if existing:
                pid = existing['id']
                stats['patients_existing'] += 1
            else:
                cur = conn.execute(
                    'INSERT INTO patients (name, diagnosis, icd_code) VALUES (?,?,?)',
                    (p['name'], p.get('diagnosis', ''), p.get('icd_code', ''))
                )
                pid = cur.lastrowid
                stats['patients_new'] += 1

            existing_dates = {r['date'] for r in conn.execute(
                'SELECT date FROM measurements WHERE patient_id=?', (pid,)).fetchall()}

            for m in (p.get('measurements') or []):
                if m['date'] in existing_dates:
                    continue  # Messung bereits vorhanden – überspringen

                # Therapeut-ID auflösen
                tid = None
                if m.get('therapist_id'):
                    # Therapeuten-Name aus Exportdaten ermitteln
                    src_t = next((t for t in (data.get('therapists') or [])
                                  if str(t.get('id')) == str(m['therapist_id']) or t.get('id') == m['therapist_id']), None)
                    if src_t:
                        row = conn.execute(
                            'SELECT id FROM therapists WHERE first_name=? AND last_name=?',
                            (src_t['first_name'], src_t['last_name'])
                        ).fetchone()
                        if row:
                            tid = row['id']

                cur2 = conn.execute(
                    'INSERT INTO measurements (patient_id, date, sane, pain_nrs, gpc, notes, therapist_id) VALUES (?,?,?,?,?,?,?)',
                    (pid, m['date'], m.get('sane'), m.get('pain_nrs'), m.get('gpc'), m.get('notes', ''), tid)
                )
                mid = cur2.lastrowid

                for ps in (m.get('psfs') or []):
                    conn.execute(
                        'INSERT INTO psfs_scores (measurement_id, activity_name, score) VALUES (?,?,?)',
                        (mid, ps['activity_name'], ps['score'])
                    )
                for ft in (m.get('function_tests') or []):
                    conn.execute(
                        'INSERT INTO function_tests (measurement_id, test_name, test_value, test_unit) VALUES (?,?,?,?)',
                        (mid, ft['test_name'], ft['test_value'], ft.get('test_unit', ''))
                    )
                stats['measurements_new'] += 1

        conn.commit()
        return jsonify({'success': True, 'stats': stats})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ── Konfiguration (API-Schlüssel) ──────────────────────────────────────────────

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_config(cfg):
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)


@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    if request.method == 'GET':
        cfg = load_config()
        key = cfg.get('openai_key', '')
        return jsonify({
            'has_key': bool(key),
            'key_preview': f'sk-...{key[-6:]}' if len(key) > 6 else ('vorhanden' if key else ''),
            'model': cfg.get('model', 'gpt-4o'),
        })
    data = request.get_json()
    cfg = load_config()
    if 'openai_key' in data:
        cfg['openai_key'] = data['openai_key'].strip()
    if 'model' in data:
        cfg['model'] = data['model']
    save_config(cfg)
    return jsonify({'success': True})


# ── Berichtgenerierung (SSE-Streaming) ─────────────────────────────────────────

def build_prompt(patient, measurements, instructions='', report_type='standard'):
    """Erstellt den strukturierten Prompt für den Therapiebericht."""
    first, last = measurements[0], measurements[-1]

    GPC_TEXT = {
        3: 'Vollständig wiederhergestellt',
        2: 'Deutliche Verbesserung',
        1: 'Geringe Verbesserung',
        0: 'Unverändert',
        -1: 'Geringe Verschlechterung',
        -2: 'Deutliche Verschlechterung',
        -3: 'Schlechter als je zuvor',
    }

    INTRO = {
        'standard':      'Erstelle einen professionellen Physiotherapie-Therapiebericht (Arztbrief) auf Deutsch\nfür den behandelnden Arzt. Verwende eine sachliche, medizinische Sprache.',
        'summary':       'Erstelle eine kurze Zusammenfassung (3–5 Sätze) des Therapieverlaufs auf Deutsch.\nFasse die wichtigsten Ergebnisse prägnant zusammen. Keine Überschriften, kein Briefformat.',
        'soap':          'Erstelle eine strukturierte Verlaufsnotiz im SOAP-Format auf Deutsch.\nGliederung: S (Subjektiv) – O (Objektiv) – A (Assessment) – P (Plan).',
        'pain_function': 'Erstelle einen professionellen Physiotherapie-Therapiebericht auf Deutsch\nmit besonderem Fokus auf die Schmerzentwicklung und Funktionsverbesserung.',
    }

    lines = [
        INTRO.get(report_type, INTRO['standard']),
        '',
        '══ PATIENTENDATEN ══',
        f'Name/Code : {patient["name"]}',
        f'ICD-10    : {patient["icd_code"] or "nicht angegeben"}',
        f'Diagnose  : {patient["diagnosis"] or "nicht dokumentiert"}',
        f'Zeitraum  : {first["date"]} bis {last["date"]} ({len(measurements)} Messzeitpunkt{"e" if len(measurements)>1 else ""})',
        '',
        '══ MESSWERTE (PAT_S – Physio-Akademie Tool Schulter) ══',
        'Erläuterung der Instrumente:',
        '  SANE  = Selbsteinschätzung Schulter in % (0=funktionslos, 100=normal); Schwellenwert ±15 %',
        '  NRS   = Schmerzintensität 0–10 (0=kein Schmerz, 10=maximal); Schwellenwert ±2',
        '  GPC   = Globale Veränderungseinschätzung −3 bis +3; Schwellenwert ±1',
        '  PSFS  = Patientenspezifische Funktionsskala 0–10 je Aktivität; Summenschwellenwert ±2',
        '',
    ]

    for m in measurements:
        therapist_info = f'  (Therapeut: {m["therapist_name"]})' if m.get('therapist_name') else ''
        lines.append(f'── Messung {m["date"]}{therapist_info} ──')
        if m['sane'] is not None:
            lines.append(f'  SANE   : {m["sane"]} %')
        if m['pain_nrs'] is not None:
            lines.append(f'  NRS    : {m["pain_nrs"]}/10')
        if m['gpc'] is not None:
            lines.append(f'  GPC    : {m["gpc"]:+d}  ({GPC_TEXT.get(m["gpc"], "")})')
        if m['psfs']:
            total = sum(p['score'] for p in m['psfs'])
            lines.append(f'  PSFS ∑ : {total}')
            for p in m['psfs']:
                lines.append(f'    • {p["activity_name"]}: {p["score"]}/10')
        if m['function_tests']:
            for t in m['function_tests']:
                lines.append(f'  {t["test_name"]}: {t["test_value"]} {t["test_unit"]}')
        if m['notes']:
            lines.append(f'  Notizen: {m["notes"]}')
        lines.append('')

    # Veränderungen zusammenfassen
    if len(measurements) >= 2:
        lines += ['══ VERÄNDERUNGEN (Eingang → Aktuell) ══']
        for key, label, thr, unit in [
            ('sane', 'SANE', 15, '%'),
            ('pain_nrs', 'NRS', 2, ''),
            ('gpc', 'GPC', 1, ''),
        ]:
            fv, lv = first.get(key), last.get(key)
            if fv is not None and lv is not None:
                diff = lv - fv
                sig = 'klinisch relevant' if abs(diff) >= thr else 'unter Schwellenwert'
                lines.append(f'  {label}: {fv}{unit} → {lv}{unit}  (Δ {diff:+}  – {sig})')

        # PSFS Summenscore-Vergleich + Einzelaktivitäten
        ps_first = sum(p['score'] for p in (first.get('psfs') or []))
        ps_last  = sum(p['score'] for p in (last.get('psfs') or []))
        if first.get('psfs') and last.get('psfs'):
            diff = ps_last - ps_first
            sig = 'klinisch relevant' if abs(diff) >= 2 else 'unter Schwellenwert'
            lines.append(f'  PSFS ∑: {ps_first} → {ps_last}  (Δ {diff:+}  – {sig})')
            first_psfs = {p['activity_name']: p['score'] for p in first.get('psfs', [])}
            last_psfs  = {p['activity_name']: p['score'] for p in last.get('psfs',  [])}
            for name in first_psfs:
                if name in last_psfs:
                    d = last_psfs[name] - first_psfs[name]
                    lines.append(f'    • {name}: {first_psfs[name]} → {last_psfs[name]}  (Δ {d:+})')
        lines.append('')

    TASK = {
        'standard': [
            '══ AUFGABE ══',
            'Erstelle einen strukturierten Arztbrief mit diesen Abschnitten:',
            '1. Betreff (Diagnose, Behandlungszeitraum)',
            '2. Vorstellungsgrund und Ausgangssituation',
            '3. Therapieverlauf und Ergebnismessung (beziehe die konkreten Messwerte ein)',
            '4. Aktueller Befund',
            '5. Zusammenfassung und Empfehlung',
            '',
            'Interpretiere die Messwerte klinisch. Schwellenwerte basieren auf dem IQWiG-Standard (15% der Skalaspanne).',
            'Schreibe in der dritten Person (z.B. "Der Patient …" / "Die Patientin …").',
        ],
        'summary': [
            '══ AUFGABE ══',
            'Schreibe 3–5 zusammenhängende Sätze. Kein Briefformat, keine Überschriften.',
            'Nenne Diagnose, Behandlungszeitraum, die wichtigsten Messwertveränderungen und das Ergebnis.',
            'Interpretiere die Messwerte klinisch (klinisch relevant / unter Schwellenwert).',
            'Schreibe in der dritten Person.',
        ],
        'soap': [
            '══ AUFGABE ══',
            'Erstelle eine Verlaufsnotiz exakt im SOAP-Format:',
            'S (Subjektiv):  Beschwerden und Selbsteinschätzung des Patienten (SANE, NRS, PSFS, GPC).',
            'O (Objektiv):   Messwerte und Funktionstests mit konkreten Zahlen.',
            'A (Assessment): Klinische Interpretation der Veränderungen (Schwellenwerte beachten).',
            'P (Plan):       Empfehlung für das weitere Vorgehen.',
            '',
            'Keine zusätzlichen Abschnitte außerhalb des SOAP-Schemas.',
            'Schreibe in der dritten Person.',
        ],
        'pain_function': [
            '══ AUFGABE ══',
            'Erstelle einen strukturierten Arztbrief mit diesen Abschnitten:',
            '1. Betreff (Diagnose, Behandlungszeitraum)',
            '2. Schmerzentwicklung: Analysiere NRS- und SANE-Verlauf detailliert.',
            '3. Funktionsverbesserung: Analysiere PSFS und Funktionstests detailliert.',
            '4. GPC und Gesamtbeurteilung',
            '5. Zusammenfassung und Empfehlung',
            '',
            'Stelle Schmerz- und Funktionsdaten in den Vordergrund. Interpretiere klinische Relevanz anhand der Schwellenwerte.',
            'Schreibe in der dritten Person.',
        ],
    }

    lines += TASK.get(report_type, TASK['standard'])

    if instructions:
        lines += ['', f'Zusätzliche Hinweise: {instructions}']

    return '\n'.join(lines)


@app.route('/api/generate-report', methods=['POST'])
def generate_report():
    cfg = load_config()
    api_key = cfg.get('openai_key', '')
    if not api_key:
        return jsonify({'error': 'OpenAI API-Schlüssel nicht konfiguriert'}), 400

    data = request.get_json()
    pid = data.get('patient_id')
    if not pid:
        return jsonify({'error': 'patient_id fehlt'}), 400

    date_from   = data.get('date_from')
    date_to     = data.get('date_to')
    report_type = data.get('report_type', 'standard')

    conn = get_db()
    try:
        patient = conn.execute('SELECT * FROM patients WHERE id = ?', (pid,)).fetchone()
        if not patient:
            return jsonify({'error': 'Patient nicht gefunden'}), 404

        query  = '''SELECT m.*, COALESCE(t.first_name || ' ' || t.last_name, '') AS therapist_name
                    FROM measurements m
                    LEFT JOIN therapists t ON t.id = m.therapist_id
                    WHERE m.patient_id = ?'''
        params = [pid]
        if date_from:
            query += ' AND m.date >= ?'
            params.append(date_from)
        if date_to:
            query += ' AND m.date <= ?'
            params.append(date_to)
        query += ' ORDER BY m.date'

        rows = conn.execute(query, params).fetchall()

        if not rows:
            return jsonify({'error': 'Keine Messungen für diesen Patienten im gewählten Zeitraum vorhanden'}), 400

        measurements = []
        for row in rows:
            m = dict(row)
            m['psfs'] = [dict(r) for r in conn.execute(
                'SELECT * FROM psfs_scores WHERE measurement_id = ?', (m['id'],)
            ).fetchall()]
            m['function_tests'] = [dict(r) for r in conn.execute(
                'SELECT * FROM function_tests WHERE measurement_id = ?', (m['id'],)
            ).fetchall()]
            measurements.append(m)
    finally:
        conn.close()

    prompt = build_prompt(dict(patient), measurements, data.get('instructions', ''), report_type)
    model  = cfg.get('model', 'gpt-4o')

    system_msg = (
        'Du bist ein erfahrener Physiotherapeut und erstellst professionelle Therapieberichte '
        'für Ärzte. Deine Berichte sind sachlich, präzise, klinisch fundiert und auf Deutsch.'
    )

    def stream():
        try:
            client = OpenAI(api_key=api_key)
            stream = client.chat.completions.create(
                model=model,
                messages=[
                    {'role': 'system', 'content': system_msg},
                    {'role': 'user',   'content': prompt},
                ],
                temperature=0.25,
                max_tokens=2500,
                stream=True,
            )
            for event in stream:
                if hasattr(event, 'choices'):
                    # Standard ChatCompletionChunk
                    delta = event.choices[0].delta.content if event.choices else None
                elif hasattr(event, 'delta') and isinstance(event.delta, str):
                    # openai v2 ChunkEvent (type="content.delta")
                    delta = event.delta
                else:
                    continue
                if delta:
                    yield f'data: {json.dumps({"t": delta})}\n\n'
            yield 'data: [DONE]\n\n'
        except Exception as exc:
            yield f'data: {json.dumps({"error": str(exc)})}\n\n'

    return Response(stream_with_context(stream()), content_type='text/event-stream')


# ── Static ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


if __name__ == '__main__':
    init_db()
    print("\n" + "=" * 60)
    print("  PAT_S – Physio-Akademie Tool Schulter")
    print("  Server läuft auf: http://localhost:5000")
    print("=" * 60 + "\n")
    app.run(debug=False, port=5000)
