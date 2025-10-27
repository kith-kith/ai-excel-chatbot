import os
import re
import pandas as pd
from flask import Flask, request, jsonify, render_template, session
import cohere  # Changed from 'google.generativeai'
from pandasql import sqldf
from dotenv import load_dotenv
import numpy as np
import warnings

# Load environment variables from .env file
# Make sure you have a .env file with your COHERE_API_KEY
load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Initialize Cohere Client
try:
    # Configure the client with your API key from the environment
    co = cohere.Client(os.environ.get("COHERE_API_KEY"))
    COHERE_API_KEY_CONFIGURED = True
    print("Cohere API key configured successfully.")
except Exception as e:
    print(f"Warning: Cohere API key not configured. {e}")
    COHERE_API_KEY_CONFIGURED = False

# Global variable to hold the dataframe
df = None

def pysqldf(q):
    """Allows running SQL queries on the DataFrame."""
    return sqldf(q, globals())

def get_column_info(dataframe):
    """Generates a string describing the DataFrame's schema."""
    return "\n".join([f"- '{col}' (type: {dataframe[col].dtype})" for col in dataframe.columns])

# <<< START: THE DEFINITIVE FIX FOR ALL TEXT SEARCHES >>>
def get_system_prompt(file_schema, dataframe):
    """
    This is the final, most robust prompt. It includes a new, high-priority
    section to ensure all text searches are handled correctly and reliably.
    """
    summary_parts = [f"This dataset contains {dataframe.shape[0]} records and {dataframe.shape[1]} columns."]
    dept_col = next((col for col in dataframe.columns if 'dept' in col.lower()), None)
    if dept_col:
        summary_parts.append(f"- **Top Department:** The department with the most employees is '{dataframe[dept_col].mode()[0]}'.")

    sal_col = next((col for col in dataframe.columns if 'sal' in col.lower()), None)
    if sal_col and pd.api.types.is_numeric_dtype(dataframe[sal_col]):
        summary_parts.append(f"- **Salary Range:** Gross salaries range from {dataframe[sal_col].min():,.2f} to {dataframe[sal_col].max():,.2f}.")

    smart_summary_code = f'`"{"\\n".join(summary_parts)}"`'

    # This prompt structure is now for Cohere's 'preamble'
    return f"""
        You are a master data analyst AI. Your only purpose is to generate a single, correct line of Python code to answer a user's question about a pandas DataFrame named `df`.

        **THE MOST IMPORTANT RULE OF ALL:**
        - Your response MUST BE a single line of pure, valid Python code and NOTHING ELSE.
        - DO NOT include `python`, backticks, explanations, or any conversational text.

        The DataFrame `df` has this schema:
        {file_schema}
        **IMPORTANT:** Columns for names (`clean_emp_name`) and salaries have been pre-cleaned. Use them directly.

        ---
        **HIERARCHY & LOGIC:**

        **1. CRITICAL TEXT SEARCH RULES (NEW - FOLLOW THESE ALWAYS):**
           - For ANY text search (`startswith`, `contains`, `endswith`), you **MUST** use the `clean_emp_name` column.
           - The text you are searching for **MUST** be converted to lowercase in your code.
           - **"list names that start with M" ->** `df[df['clean_emp_name'].str.startswith('m', na=False)]['emp_name'].tolist()`
           - **"who have george in their name" ->** `df[df['clean_emp_name'].str.contains('george', na=False)]`
           - **"find people whose name ends with 'sh'" ->** `df[df['clean_emp_name'].str.endswith('sh', na=False)]`

        **2. FULL RECORD LOOKUP:**
           - If the user asks for *full details* about a person (e.g., "tell me about", "details for"), you **MUST** return the entire filtered DataFrame.
           - **"tell me about jayaraj" ->** `df[df['clean_emp_name'].str.contains('jayaraj', na=False)]`

        **3. NUMERIC FILTERING (SIMPLE & DIRECT):**
           - **"List people who earn more than 40000" ->** `df.query('gross_sal > 40000')`

        **4. COUNTING UNIQUE VALUES:**
           - **"List the unique blood groups and their counts." ->** `df['blood_group'].value_counts().reset_index()`

        **5. GENERAL QUERIES:**
           - **"How many records?" ->** `f"There are {len(df)} records in total."`
           - **"Give me a summary." ->** {smart_summary_code}
        ---
        """
# <<< END: THE DEFINITIVE FIX FOR ALL TEXT SEARCHES >>>


@app.route('/')
def index():
    session.clear()
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    global df
    if 'file' not in request.files: return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({"error": "No selected file"}), 400

    if file and (file.filename.endswith('.xlsx') or file.filename.endswith('.csv')):
        try:
            df = pd.read_csv(file, encoding='utf-8', low_memory=False) if file.filename.endswith('.csv') else pd.read_excel(file)
            df.columns = df.columns.str.strip().str.lower()

            name_col = next((col for col in df.columns if 'name' in col), None)
            if name_col:
                print(f"Cleaning name column: '{name_col}'")
                df['clean_emp_name'] = df[name_col].astype(str).str.replace(r'^(mr\.?|ms\.?|mrs\.?|dr\.?|miss|m/s)\s*', '', regex=True, case=False).str.strip().str.lower()

            for col in df.columns:
                if 'sal' in col or 'amount' in col or 'price' in col or 'cost' in col or 'value' in col:
                    if df[col].dtype == 'object':
                        print(f"Detected non-numeric salary/value column '{col}'. Cleaning now.")
                        df[col] = df[col].astype(str).str.replace(r'[^\d.]', '', regex=True)
                        df[col] = pd.to_numeric(df[col], errors='coerce')
                        print(f"Successfully cleaned and converted '{col}' to numeric.")

            session.clear()
            session['chat_history'] = []
            session['file_schema'] = get_column_info(df)
            return jsonify({"success": f"File '{file.filename}' uploaded and prepared successfully."})
        except Exception as e:
            return jsonify({"error": f"Error processing file: {e}"}), 500
    return jsonify({"error": "Invalid file type."}), 400

@app.route('/chat', methods=['POST'])
def chat():
    global df
    if not COHERE_API_KEY_CONFIGURED: return jsonify({"error": "Backend AI is not configured."}), 500
    if df is None or 'file_schema' not in session: return jsonify({"error": "Please upload a file first."}), 400
    user_question = request.json.get('question')
    if not user_question: return jsonify({"error": "No question provided."}), 400

    system_prompt = get_system_prompt(session['file_schema'], df)

    # Get chat history from session and format for Cohere API
    chat_history = session.get('chat_history', [])
    cohere_history = []
    for message in chat_history:
        role = 'CHATBOT' if message['role'] == 'assistant' else 'USER'
        cohere_history.append({'role': role, 'message': message['content']})

    try:
        # Send the user's new question to Cohere
        response = co.chat(
            message=user_question,
            preamble=system_prompt,
            chat_history=cohere_history[-4:], # Use the last few interactions for context
            temperature=0 # For deterministic code generation
        )

        generated_code = response.text.strip().replace('`', '')

        try:
            result = eval(generated_code, {'df': df, 'pd': pd, 'np': np})
        except SyntaxError:
            print(f"Initial eval failed. AI response: '{generated_code}'. Attempting to extract code.")
            match = re.search(r"(df\.query\(.*\)|df\[.*\]|len\(.*\)|f?\".*?\")", generated_code)
            if match:
                extracted_code = match.group(1)
                print(f"Successfully extracted code: {extracted_code}")
                result = eval(extracted_code, {'df': df, 'pd': pd, 'np': np})
            else:
                raise ValueError(f"Could not extract valid Python from AI response: {generated_code}")

        except Exception as e:
            return jsonify({"answer": f"I tried this code:<br><code>{generated_code}</code><br><br>But it failed: `{e}`<br>Please try rephrasing."})

        response_html = ""
        if isinstance(result, pd.DataFrame):
            if result.empty:
                sal_col_in_df = next((col for col in df.columns if 'sal' in col.lower()), None)
                if sal_col_in_df and sal_col_in_df in generated_code and pd.api.types.is_numeric_dtype(df[sal_col_in_df]):
                   max_sal = df[sal_col_in_df].max()
                   response_html = f"I couldn't find any records that match your filter. For context, the highest salary in the dataset is **{max_sal:,.2f}**."
                else:
                    response_html = "I couldn't find any records that match your query."
            else:
                response_html = "<div class='table-responsive'>" + result.to_html(classes='table table-striped', index=False) + "</div>"

        elif isinstance(result, pd.Series):
            series_df = result.reset_index()
            series_df.columns = [result.index.name if result.index.name else 'Value', result.name if result.name else 'Count']
            response_html = "<div class='table-responsive'>" + series_df.to_html(classes='table table-striped', index=False) + "</div>"

        elif isinstance(result, list):
            if not result:
                response_html = "I couldn't find any results for that."
            else:
                list_df = pd.DataFrame(result, columns=['Results'])
                response_html = "<div class='table-responsive'>" + list_df.to_html(classes='table table-striped', index=False) + "</div>"

        else:
            response_html = str(result).replace('\n', '<br>')

        # Keep using 'assistant' role for session storage to maintain internal consistency
        chat_history.append({"role": "user", "content": user_question})
        chat_history.append({"role": "assistant", "content": generated_code})
        session['chat_history'] = chat_history
        return jsonify({"answer": response_html})

    except Exception as e:
        return jsonify({"answer": f"An unexpected error occurred. Please try again. (Error: {e})"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)