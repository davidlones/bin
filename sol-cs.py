from flask import Flask, request, jsonify, make_response
import openai
import os
import subprocess
from flask_cors import CORS
from werkzeug.serving import run_simple

# Set your OpenAI API key
openai.api_key = os.getenv('OPENAI_API_KEY')

app = Flask(__name__)
CORS(app, origins=["https://davidlones.github.io"])

@app.route('/api', methods=['POST', 'OPTIONS'])
def chat():
    if request.method == 'OPTIONS':
        # This is a preflight request. Reply successfully:
        response = make_response()
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add('Access-Control-Allow-Headers', "*")
        response.headers.add('Access-Control-Allow-Methods', "*")
        return response

    # This is the actual request. Handle it as usual:
    if request.is_json:
        data = request.get_json()
        message = data.get('message', '')  # Use empty string as default value if 'message' is not provided

        # load services.html from file
        with open('../davidlones.github.io/services.html', 'r') as file:
            services_html = file.read()

        # Call OpenAI API here with the message
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are Sol, a helpful assistant designed by David to help him help others. You take a customers description of their problem and respond with the body of an email detailing the issue and what services David can provide."},
                {"role": "assistant", "content": f"I will take the customers description of their problem and respond with the well formated body of an email to send to David, detailing a description of the customers issue, a list of possible services we could provide based on their issues and the content our webpage ({services_html}), and the dollar amount we could charge the customer for our services, to be sent to David to review later today."},
                {"role": "user", "content": f"A customer has written to us concerning the following: '{message}'. Write an email to David using the following email format: '\n\nHi David,\n\nI hope you are having a great day!\n\nI am writing to you today because I have a customer who is experiencing the following issue:\n\n<summerization of the customers description of their issue>\n\nI believe we could provide the following services to help them:\n\n<list of services the we offer that apply to the customers needs>\n\nI believe we could charge the customer the following amount for our services:\n\n<amount>\n\nPlease review this request and let me know what you think.\n\nBest,\n\nYour Helpful Assistant, Sol\n\n'"},
            ]
        )
        assistant_message = response['choices'][0]['message']['content']
        print("User Message: ", message)
        print("API Response: ", assistant_message) # print API response to terminal

        response = jsonify({'message': assistant_message})
        response.headers.add("Access-Control-Allow-Origin", "*")
        return response
    else:
        return make_response(jsonify({'error': 'Invalid request'}), 415)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8042)