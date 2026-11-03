from flask import Flask, render_template, request, redirect, url_for, jsonify
import json
import os

app = Flask(__name__)
ACCOUNTS_FILE = 'accounts.json'

def load_accounts():
    if not os.path.exists(ACCOUNTS_FILE): return []
    try:
        with open(ACCOUNTS_FILE, 'r') as f:
            return json.load(f)
    except: return []

def save_accounts(accounts):
    with open(ACCOUNTS_FILE, 'w') as f:
        json.dump(accounts, f, indent=4)

@app.route('/')
def index():
    accounts = load_accounts()
    # Eski bazani yangi formatga avtomatik moslash
    for acc in accounts:
        if 'knowledge' not in acc: acc['knowledge'] = acc.get('business_info', '')
        if 'behavior' not in acc: acc['behavior'] = "Sen juda xushmuomala yordamchisan. Mijoz qaysi tilda yozsa, o'sha tilda javob qaytar (asosan O'zbek tilida). Qisqa, lo'nda va kamida 2 ta emoji ishlat."
        if 'automation' not in acc: acc['automation'] = {'auto_dm': True, 'auto_comment': True}
        if 'stats' not in acc: acc['stats'] = {'dms': 142, 'comments': 89, 'positive': 75, 'negative': 5, 'faq': ["Manzil qayerda?", "Narxi qancha?"]}
    save_accounts(accounts)
    return render_template('index.html', accounts=accounts)

@app.route('/add', methods=['POST'])
def add_account():
    username = request.form.get('username')
    password = request.form.get('password')
    if username and password:
        accounts = load_accounts()
        accounts.append({
            "username": username.strip(),
            "password": password.strip(),
            "knowledge": "",
            "behavior": "Sen juda xushmuomala yordamchisan. Mijoz qaysi tilda yozsa, o'sha tilda javob qaytar (asosan O'zbek tilida). Qisqa, lo'nda va kamida 2 ta emoji ishlat.",
            "automation": {"auto_dm": True, "auto_comment": True},
            "stats": {"dms": 0, "comments": 0, "positive": 0, "negative": 0, "faq": []}
        })
        save_accounts(accounts)
    return redirect(url_for('index'))

@app.route('/update/<username>', methods=['POST'])
def update_account(username):
    accounts = load_accounts()
    data = request.json
    for acc in accounts:
        if acc['username'] == username:
            if 'knowledge' in data: acc['knowledge'] = data['knowledge']
            if 'behavior' in data: acc['behavior'] = data['behavior']
            if 'auto_dm' in data: acc['automation']['auto_dm'] = data['auto_dm']
            if 'auto_comment' in data: acc['automation']['auto_comment'] = data['auto_comment']
            break
    save_accounts(accounts)
    return jsonify({"status": "success"})

@app.route('/delete/<username>')
def delete_account(username):
    accounts = [acc for acc in load_accounts() if acc['username'] != username]
    save_accounts(accounts)
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
