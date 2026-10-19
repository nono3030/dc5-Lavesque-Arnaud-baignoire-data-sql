#!/usr/bin/env python3
# Pousse la position en UN SEUL combo Deribit (un fill, un prix net).
# Dry-run + testnet par defaut. Cle API : scope trade, wallet none, IP restreinte.
#   export DERIBIT_ID=...  DERIBIT_SECRET=...
#   python push.py                 -> affiche seulement (lecture publique)
#   python push.py --go            -> envoie sur testnet
#   python push.py --go --live     -> envoie en reel
import os, sys, time, json, requests
LIVE = '--live' in sys.argv; GO = '--go' in sys.argv
BASE = 'https://www.deribit.com' if (LIVE or not GO) else 'https://test.deribit.com'  # dry-run : lecture des vrais prix
LEGS = json.loads('''[{"type":"P","expiry":"20OCT26","delta":0.2,"direction":"sell","amount":0.02},{"type":"C","expiry":"20OCT26","delta":0.2,"direction":"sell","amount":0.02}]''')      # legs voulus ; 'delta' au lieu de 'instrument' = strike choisi dans la chaine live
PROD = 'BTC_USDC'                      # 'BTC' (options coin-margined) ou 'BTC_USDC' (options USDC)
FLOOR = 0.95                           # plancher = FLOOR x mark net, recalcule au lancement puis a chaque repricing
START = 1.00                           # prix de depart = START x mark net
STEP, EVERY, MAX_MIN = 0.01, 120, 25   # baisse de 1 % du mark toutes les 2 min, annule apres 25 min
MAX_AMOUNT = 0.02                      # garde-fou : taille max par leg, en BTC
if any(l['amount'] > MAX_AMOUNT + 1e-9 for l in LEGS): raise SystemExit('Taille > %s BTC par leg : refuse.' % MAX_AMOUNT)
tok = None
def call(path, params=None, private=False):
    h = {'Authorization': 'Bearer ' + tok} if private else {}
    body = {'jsonrpc': '2.0', 'id': 1, 'method': path, 'params': params or {}}
    r = requests.post(BASE + '/api/v2/' + path, json=body, headers=h, timeout=15).json()
    if 'error' in r: raise SystemExit('Deribit ' + path + ' : ' + json.dumps(r['error']))
    return r['result']
def tick_round(x, tick): return round(round(x / tick) * tick, 8)
def net_mark():
    tot = 0
    for l in LEGS:
        m = call('public/get_order_book', {'instrument_name': l['instrument'], 'depth': 1})
        print('  %-4s %s %s  mark %s  bid %s  ask %s' % (l['direction'], l['amount'], l['instrument'],
              m['mark_price'], m.get('best_bid_price'), m.get('best_ask_price')))
        tot += (1 if l['direction'] == 'sell' else -1) * m['mark_price']
    return tot
print('Mode :', 'LIVE' if LIVE else ('TESTNET' if GO else 'DRY-RUN (prix reels, rien envoye)'))
def resolve():
    """Legs donnes par delta cible : choisit le strike dans la vraie chaine Deribit."""
    for l in LEGS:
        if 'instrument' in l: continue
        pre = PROD + '-' + l['expiry'] + '-'
        names = [i for i in call('public/get_instruments', {'currency': 'USDC' if PROD == 'BTC_USDC' else 'BTC', 'kind': 'option'})
                 if i['instrument_name'].startswith(pre) and i['instrument_name'].endswith('-' + l['type'])]
        spot = call('public/get_index_price', {'index_name': 'btc_usd'})['index_price']
        best = None
        for i in names:
            if abs(i['strike'] / spot - 1) > 0.10: continue
            o = call('public/get_order_book', {'instrument_name': i['instrument_name'], 'depth': 1})
            if not o.get('best_bid_price'): continue
            gap = abs(abs(o['greeks']['delta']) - l['delta'])
            if best is None or gap < best[0]: best = (gap, i['instrument_name'], o['greeks']['delta'], o['mark_iv'], o['open_interest'])
        if best is None: raise SystemExit('Aucun strike avec bid pour ' + str(l))
        l['instrument'] = best[1]
        print('  delta cible %.2f -> %s (delta %.3f, IV %.1f, OI %s)' % (l['delta'], best[1], best[2], best[3], best[4]))
resolve()
credit_mark = net_mark()
MIN_NET = credit_mark * FLOOR
print('Credit net au mark : %.4f (unite du produit) par BTC | plancher accepte : %.4f' % (credit_mark, MIN_NET))
print('Prix de depart : %.4f (START %.2f) | duree max %d min' % (credit_mark * START, START, MAX_MIN))
if credit_mark * START < MIN_NET: raise SystemExit('Prix de depart sous le plancher : abandon.')
if not GO: sys.exit('Dry-run termine, rien envoye.')
auth = call('public/auth', {'grant_type': 'client_credentials', 'client_id': os.environ['DERIBIT_ID'],
                            'client_secret': os.environ['DERIBIT_SECRET']})
tok = auth['access_token']
if auth['expires_in'] < MAX_MIN * 60 + 120: raise SystemExit('Jeton valide %s s seulement, trop court pour %d min : abandon.' % (auth['expires_in'], MAX_MIN))
amount = LEGS[0]['amount']
if any(abs(l['amount'] - amount) > 1e-9 for l in LEGS): raise SystemExit('Legs de tailles differentes : non supporte.')
combo = call('private/create_combo', {'trades': [{'instrument_name': l['instrument'],
             'amount': l['amount'], 'direction': l['direction']} for l in LEGS]}, private=True)
cid = combo['id']; print('Combo :', cid, '| etat', combo['state'])
# sens du combo : on le deduit des legs renvoyes (leg > 0 = meme sens que l'ordre sur le combo)
signs = {}
for cl in combo['legs']:
    want = [l for l in LEGS if l['instrument'] == cl['instrument_name']][0]['direction']
    same = cl['amount'] > 0
    signs[cl['instrument_name']] = want if same else ('buy' if want == 'sell' else 'sell')
dirs = set(signs.values())
if len(dirs) != 1: raise SystemExit('Sens du combo ambigu, abandon : ' + str(signs))
cdir = dirs.pop()
tick = call('public/get_instrument', {'instrument_name': cid})['tick_size']
cm = call('public/get_order_book', {'instrument_name': cid, 'depth': 1})
print('Carnet combo : bid %s ask %s mark %s' % (cm.get('best_bid_price'), cm.get('best_ask_price'), cm['mark_price']))
# prix du combo p : en vendant le combo on encaisse p ; en l'achetant on paie p (credit = -p)
to_p = (lambda c: c) if cdir == 'sell' else (lambda c: -c)
credit = credit_mark * START
price = tick_round(to_p(credit), tick)
oid = call('private/' + cdir, {'instrument_name': cid, 'amount': amount, 'type': 'limit', 'price': price,
           'label': 'combo-push'}, private=True)['order']['order_id']
t0 = time.time(); last = t0
n = 0  # nombre de baisses
print('Ordre %s a %s (credit %.4f)' % (oid, price, credit))
try:
    while True:
        time.sleep(10)
        st = call('private/get_order_state', {'order_id': oid}, private=True)
        if st['order_state'] == 'filled':
            print('FILLE a', st.get('average_price')); break
        if st['order_state'] in ('cancelled', 'rejected'): print('Etat', st['order_state']); break
        if time.time() - t0 > MAX_MIN * 60:
            call('private/cancel', {'order_id': oid}, private=True); print('Timeout, ordre annule.'); break
        if time.time() - last >= EVERY:
            last = time.time()
            n += 1
            mark_now = net_mark()
            floor = mark_now * FLOOR
            credit = max(mark_now * (START - STEP * n), floor)
            new = tick_round(to_p(credit), tick)
            if to_p(new) < floor - 1e-9: new = tick_round(new + (tick if cdir == 'sell' else -tick), tick)  # l'arrondi ne passe jamais sous le plancher
            if new == price: print('Reprice %d : prix inchange %s (mark %.4f, plancher %.4f)' % (n, price, mark_now, floor)); continue
            price = new
            call('private/edit', {'order_id': oid, 'amount': amount, 'price': price}, private=True)
            print('Reprice %d -> %s (credit %.4f, mark %.4f, plancher %.4f)' % (n, price, to_p(price), mark_now, floor))
except BaseException:  # erreur Deribit, reseau ou Ctrl+C : on ne laisse pas l'ordre seul dans le carnet
    try:
        call('private/cancel', {'order_id': oid}, private=True); print('Interruption : ordre', oid, 'annule.')
    except BaseException as e:
        print('ATTENTION : ordre', oid, 'peut-etre encore ouvert ->', e)
    raise
