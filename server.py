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
    ''')
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
            'INSERT INTO patients (name, diagnosis) VALUES (?, ?)',
            (name, data.get('diagnosis', '').strip())
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
                'SELECT * FROM measurements WHERE patient_id = ? ORDER BY date ASC',
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
            'INSERT INTO measurements (patient_id, date, sane, pain_nrs, gpc, notes) VALUES (?, ?, ?, ?, ?, ?)',
            (data['patient_id'], data['date'],
             data.get('sane'), data.get('pain_nrs'), data.get('gpc'),
             data.get('notes', ''))
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
            'UPDATE measurements SET date=?, sane=?, pain_nrs=?, gpc=?, notes=? WHERE id=?',
            (data['date'], data.get('sane'), data.get('pain_nrs'), data.get('gpc'),
             data.get('notes', ''), mid)
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

def build_prompt(patient, measurements, instructions=''):
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

    lines = [
        'Erstelle einen professionellen Physiotherapie-Therapiebericht (Arztbrief) auf Deutsch',
        'für den behandelnden Arzt. Verwende eine sachliche, medizinische Sprache.',
        '',
        '══ PATIENTENDATEN ══',
        f'Name/Code : {patient["name"]}',
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
        lines.append(f'── Messung {m["date"]} ──')
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

    lines += [
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
    ]

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

    conn = get_db()
    try:
        patient = conn.execute('SELECT * FROM patients WHERE id = ?', (pid,)).fetchone()
        if not patient:
            return jsonify({'error': 'Patient nicht gefunden'}), 404

        rows = conn.execute(
            'SELECT * FROM measurements WHERE patient_id = ? ORDER BY date', (pid,)
        ).fetchall()

        if not rows:
            return jsonify({'error': 'Keine Messungen für diesen Patienten vorhanden'}), 400

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

    prompt = build_prompt(dict(patient), measurements, data.get('instructions', ''))
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
