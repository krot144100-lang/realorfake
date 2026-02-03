from flask import Flask, render_template, request, jsonify
import time
import random

app = Flask(__name__)

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/analyze', methods=['POST'])
def analyze():
    try:
        data = request.get_json()
        url = data.get('url', '').lower()
        
        # 🧪 SIMULATE PROCESSING TIME (Makes it feel real)
        time.sleep(2) 

        # 🕵️‍♂️ LOGIC ENGINE (Heuristics)
        
        # 1. Known AI Platforms (Always Fake)
        ai_keywords = ['kling', 'luma', 'sora', 'runway', 'midjourney', 'haiper', 'synthesia', 'heygen']
        if any(keyword in url for keyword in ai_keywords):
            return jsonify({'result': 'fake', 'percent': random.randint(96, 99)})

        # 2. X.com / Twitter Logic (High probability of bots/AI)
        if 'x.com' in url or 'twitter.com' in url:
            # We skew the randomizer towards FAKE for X.com because it has many bots
            # 70% Chance to say FAKE
            is_fake = random.random() > 0.3 
            result = 'fake' if is_fake else 'real'
            percent = random.randint(82, 94)
            return jsonify({'result': result, 'percent': percent})

        # 3. Instagram / TikTok Logic
        if 'instagram.com' in url or 'tiktok.com' in url:
            # 50/50 Chance
            is_fake = random.random() > 0.5
            result = 'fake' if is_fake else 'real'
            percent = random.randint(75, 95)
            return jsonify({'result': result, 'percent': percent})

        # 4. Default Fallback
        is_fake = random.random() > 0.5
        result = 'fake' if is_fake else 'real'
        percent = random.randint(70, 90)
        
        return jsonify({'result': result, 'percent': percent})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
