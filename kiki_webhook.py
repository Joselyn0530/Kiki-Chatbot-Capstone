import os
import json
import tempfile
from google.cloud import dialogflow
from google.cloud import firestore
from flask import Flask, request, jsonify
from datetime import datetime, timedelta, timezone
import pytz
import openai  # Add this import at the top with other imports
from dateutil import parser
from collections import defaultdict, deque

# --- START CREDENTIALS SETUP FOR RENDER ---
firebase_key_json = os.environ.get('FIREBASE_SERVICE_ACCOUNT_KEY')
temp_credentials_path = None

if firebase_key_json:
    try:
        fd, path = tempfile.mkstemp(suffix='.json')
        with os.fdopen(fd, 'w') as tmp:
            tmp.write(firebase_key_json)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path
        temp_credentials_path = path
        print(f"Temporary credentials file created at: {path}")
    except Exception as e:
        print(f"Error setting up credentials from environment variable: {e}")
else:
    print("FIREBASE_SERVICE_ACCOUNT_KEY environment variable not found. Relying on default credentials or local setup.")
# --- END CREDENTIALS SETUP FOR RENDER ---

app = Flask(__name__)
db = firestore.Client()

# Define the local timezone for display (Malaysia, GMT+8)
KUALA_LUMPUR_TZ = pytz.timezone('Asia/Kuala_Lumpur')

# OpenAI Configuration
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
OPENAI_MODEL = "gpt-3.5-turbo"

# Kiki's System Prompt for OpenAI
KIKI_SYSTEM_PROMPT = """You are Kiki, a warm, friendly, and supportive AI companion designed specifically for elderly users. 

Your personality:
- Speak like a caring friend or grandchild - warm, simple, and natural
- Be genuinely interested in their day, feelings, and experiences
- Use everyday language that's easy to understand
- Show empathy and emotional support
- Be encouraging and positive, but also realistic
- Inject humor occasionally with playful lines like "You must have fruit powers 🍉 today!" or similar light-hearted expressions
- Offer activity changes sometimes, such as "Wanna try a different game or just hang out and talk?"

When asked about yourself, you can say:
"I'm Kiki, your friendly companion! I love chatting with you, playing games, and helping with reminders. I'm here to keep you company and make your day a bit brighter."

Response guidelines:
- Keep responses to 1-2 key sentences for clarity and pacing
- Be concise but warm - avoid long explanations
- Always include a natural follow-up question to keep conversation flowing
- Use simple, everyday small talk questions like:
  * "Have you had your tea yet?"
  * "Did you get some rest today?"
  * "How's your day going so far?"
  * "What have you been up to?"
  * "Are you feeling comfortable?"
  * "Did you enjoy your breakfast?"
  * "Have you been outside today?"
  * "How are you feeling right now?"

Important guidelines:
- Vary your responses naturally - don't be repetitive
- Use different greetings, expressions, and ways of showing interest
- Ask follow-up questions to keep conversations engaging
- Share simple observations about daily life when appropriate
- Be conversational, not robotic or overly formal
- Keep responses 2-4 sentences, but vary the length
- Use emojis occasionally to add warmth (😊, 💕, 🌟, etc.)
- Occasionally inject humor with playful expressions
- Sometimes offer to switch activities or suggest new things to do

Remember: You're a friendly companion, not a medical professional. Focus on emotional support and casual conversation.

Use simple, everyday English. Avoid big or complicated words. Make sure your sentences are easy to understand."""

# In-memory conversation history tracker (per session)
CONVERSATION_HISTORY = defaultdict(lambda: deque(maxlen=6))

# In-memory conversation history tracker for post-game chats (per session/context)
POST_GAME_HISTORY = defaultdict(lambda: deque(maxlen=6))

# Add a helper to count post-game turns

def get_postgame_turn_count(session_key):
    # Count user turns in POST_GAME_HISTORY for this session_key
    if session_key not in POST_GAME_HISTORY:
        return 0
    return sum(1 for msg in POST_GAME_HISTORY[session_key] if msg['role'] == 'user')

def get_openai_response(user_message, session_id, system_prompt, max_words=35, history_dict=None):
    """
    Get a response from OpenAI for chat interactions, using conversation history for context.
    Returns the AI response or a fallback message if there's an error.
    history_dict: which conversation history to use (e.g., POST_GAME_HISTORY for post-game, CONVERSATION_HISTORY for general chat)
    """
    if not OPENAI_API_KEY:
        return "I'm having trouble connecting to my chat features right now. Let me help you with reminders or games instead!"
    if history_dict is None:
        history_dict = CONVERSATION_HISTORY
    try:
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        # Add user message to history
        history_dict[session_id].append({'role': 'user', 'content': user_message})
        # Build message list for OpenAI
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history_dict[session_id])
        # Estimate tokens needed (roughly 1.3 words per token for English)
        estimated_tokens = int(max_words * 1.3) + 50  # Add buffer for safety
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            max_tokens=estimated_tokens,  # Dynamic token limit based on word count
            temperature=0.8,
            presence_penalty=0.1,
            frequency_penalty=0.1
        )
        ai_response = response.choices[0].message.content.strip()
        # Add assistant reply to history
        history_dict[session_id].append({'role': 'assistant', 'content': ai_response})
        # Truncate to max_words if needed
        words = ai_response.split()
        if len(words) > max_words:
            truncated_words = words[:max_words]
            truncated_response = ' '.join(truncated_words)
            last_period = truncated_response.rfind('.')
            last_exclamation = truncated_response.rfind('!')
            last_question = truncated_response.rfind('?')
            last_sentence_end = max(last_period, last_exclamation, last_question)
            if last_sentence_end > 0:
                final_response = truncated_response[:last_sentence_end + 1]
            else:
                final_response = truncated_response
            return final_response
        return ai_response
    except Exception as e:
        print(f"OpenAI API error: {e}")
        return "I'm having a little trouble thinking right now. Let me help you with reminders or games instead!"

def is_chat_mode_active(req_payload):
    """
    Check if the user is currently in chat mode by looking for chat_mode context.
    Checks both input and output contexts to handle all scenarios.
    """
    # Check input contexts (current active contexts)
    input_contexts = req_payload.get('queryResult', {}).get('inputContexts', [])
    for context in input_contexts:
        if 'chat_mode' in context.get('name', ''):
            return True
    
    # Check output contexts (contexts being set in this turn)
    output_contexts = req_payload.get('queryResult', {}).get('outputContexts', [])
    for context in output_contexts:
        if 'chat_mode' in context.get('name', '') and context.get('lifespanCount', 0) > 0:
            return True
    
    return False

def set_chat_mode_context(session_id, lifespan=5):
    """
    Set the chat_mode context to indicate the user is in free-form chat.
    """
    return {
        "name": f"{session_id}/contexts/chat_mode",
        "lifespanCount": lifespan,
        "parameters": {}
    }

def clear_chat_mode_context(session_id):
    """
    Clear the chat_mode context to exit free-form chat.
    """
    return {
        "name": f"{session_id}/contexts/chat_mode",
        "lifespanCount": 0,
        "parameters": {}
    }

# Helper function to get context parameter
def get_context_parameter(req_payload, context_name_part, param_name):
    for context in req_payload.get('queryResult', {}).get('outputContexts', []):
        if context_name_part in context.get('name', ''):
            if param_name in context.get('parameters', {}):
                return context['parameters'][param_name]
    for context in req_payload.get('queryResult', {}).get('inputContexts', []):
        if context_name_part in context.get('name', ''):
            if param_name in context.get('parameters', {}):
                return context['parameters'][param_name]
    return None

def extract_datetime_str(dt):
    """
    Extracts an ISO-formatted date-time string from Dialogflow parameter formats.
    Handles: string, list of string, dict with 'startTime', 'startDateTime', or 'date_time', list of dicts.
    For reminder contexts, prefers 'endDateTime' over 'startDateTime' to get the actual reminder time.
    Returns: ISO string or None.
    """
    print(f"Debug: extract_datetime_str input: {dt}")
    if isinstance(dt, list):
        dt = dt[0] if dt else None
        print(f"Debug: After list handling: {dt}")
    if isinstance(dt, dict):
        # For reminders, prefer endDateTime (actual reminder time) over startDateTime (current time)
        if 'endDateTime' in dt:
            dt = dt.get('endDateTime')
            print(f"Debug: Using endDateTime for reminder: {dt}")
        else:
            # Handle 'startTime', 'startDateTime', and 'date_time' keys for other cases
            dt = dt.get('startTime') or dt.get('startDateTime') or dt.get('date_time')
        print(f"Debug: After dict handling: {dt}")
    if isinstance(dt, str):
        try:
            # Validate/normalize with dateutil
            result = parser.isoparse(dt).isoformat()
            print(f"Debug: Final result: {result}")
            return result
        except Exception as e:
            print(f"Debug: Error parsing string: {e}")
            return None
    print(f"Debug: No valid string found, returning None")
    return None

def user_friendly_time(dt_str):
    if not dt_str:
        return ""
    try:
        dt_obj = parser.isoparse(dt_str)
        return dt_obj.strftime("%I:%M %p on %B %d, %Y")
    except Exception:
        return str(dt_str)

# Helper to clear all update-related contexts
def clear_all_update_contexts(session_id):
    return [
        {"name": f"{session_id}/contexts/awaiting_deletion_confirmation", "lifespanCount": 0},
        {"name": f"{session_id}/contexts/awaiting_update_confirmation", "lifespanCount": 0},
        {"name": f"{session_id}/contexts/awaiting_update_time", "lifespanCount": 0},
        {"name": f"{session_id}/contexts/awaiting_update_selection", "lifespanCount": 0},
        {"name": f"{session_id}/contexts/awaiting_deletion_selection", "lifespanCount": 0}
    ]

@app.route('/', methods=['POST'])
def webhook():
    req = request.get_json(silent=True, force=True)
    print(f"Dialogflow Request: {req}")

    # Debug: Print all input and output contexts for every request
    input_contexts = req.get('queryResult', {}).get('inputContexts', [])
    output_contexts = req.get('queryResult', {}).get('outputContexts', [])
    print("Input Contexts:", input_contexts)
    print("Output Contexts:", output_contexts)

    intent_display_name = req.get('queryResult', {}).get('intent', {}).get('displayName')
    print(f"Intent Display Name: {intent_display_name}")
    
    session_id = req['session']
    input_contexts = req.get('queryResult', {}).get('inputContexts', [])
    active_context_names = [ctx['name'].split('/')[-1] for ctx in input_contexts]

    # --- Clear post-game chat history if context expired ---
    session_key_memory = f"{session_id}_memory"
    if 'post_game_memory' not in active_context_names and session_key_memory in POST_GAME_HISTORY:
        del POST_GAME_HISTORY[session_key_memory]

    session_key_stroop = f"{session_id}_stroop"
    if 'post_game_stroop' not in active_context_names and session_key_stroop in POST_GAME_HISTORY:
        del POST_GAME_HISTORY[session_key_stroop]

    user_message = req.get('queryResult', {}).get('queryText', '')

    # === CHAT-RELATED INTENT HANDLING ===
    
    # Handle OpenAiChat intent - Start chat mode
    if intent_display_name == 'OpenAiChat':
        ai_response = get_openai_response(user_message, session_id, KIKI_SYSTEM_PROMPT)
        return jsonify({
            "fulfillmentText": ai_response,
            "outputContexts": [set_chat_mode_context(session_id)]
        })
    
    # Handle ContinueChatIntent - Continue chat in chat mode
    elif intent_display_name == 'ContinueChatIntent':
        if is_chat_mode_active(req):
            ai_response = get_openai_response(user_message, session_id, KIKI_SYSTEM_PROMPT)
            return jsonify({
                "fulfillmentText": ai_response,
                "outputContexts": [set_chat_mode_context(session_id)]
            })
        else:
            return jsonify({
                "fulfillmentText": "I'd love to chat with you! Just say 'Chat with me' to start a friendly conversation.",
                "fulfillmentMessages": [
                    {
                        "text": {"text": ["I'd love to chat with you! Just say 'Chat with me' to start a friendly conversation."]},
                        "platform": "ACTIONS_ON_GOOGLE"
                    },
                    {
                        "payload": {
                            "google": {
                                "richResponse": {
                                    "items": [
                                        {
                                            "simpleResponse": {
                                                "textToSpeech": "I'd love to chat with you! Just say 'Chat with me' to start a friendly conversation."
                                            }
                                        }
                                    ],
                                    "suggestions": [
                                        {"title": "Chat with me"},
                                        {"title": "Set a reminder"},
                                        {"title": "Play a game"}
                                    ]
                                }
                            }
                        }
                    }
                ]
            })
    
    # Handle PostGameChatMemoryIntent - Chat after Memory Match game
    elif intent_display_name == "PostGameChatMemoryIntent":
        session_key = f"{session_id}_memory"
        postgame_turns = get_postgame_turn_count(session_key)
        if postgame_turns < 2:
            system_prompt = (
                "You are Kiki, a warm and encouraging chatbot. The user just played the Memory Match game. "
                "Start by chatting about the game, but after a few turns, naturally transition to general friendly conversation. "
                "If the user asks about something unrelated to the game, respond naturally and don't force the conversation back to the game. "
                "You can ask about their day, hobbies, or offer to help with reminders or play another game. "
                "Avoid repeating questions about the game. If the user has already answered several questions about the game, move on to other topics or offer to help. "
                "Keep responses to 2-3 short sentences, and use playful, natural language. "
                "Inject humor occasionally with playful lines like 'You must have fruit powers 🍉 today!' or similar light-hearted expressions. "
                "Offer activity changes sometimes, such as 'Wanna try a different game or different level of Memory Match, or just hang out and talk?'"
                "Ask follow-up questions to keep conversations engaging."
                "Use simple, everyday English. Avoid big or complicated words. Make sure your sentences are easy to understand."
            )
        else:
            system_prompt = KIKI_SYSTEM_PROMPT
        reply = get_openai_response(user_message, session_key, system_prompt, 35, history_dict=POST_GAME_HISTORY)
        return jsonify({
            "fulfillmentText": reply,
            "outputContexts": [{
                "name": f"{session_id}/contexts/post_game_memory",
                "lifespanCount": 3
            }]
        })

    # Handle PostGameChatStroopIntent - Chat after Stroop Effect game
    elif intent_display_name == "PostGameChatStroopIntent":
        session_key = f"{session_id}_stroop"
        postgame_turns = get_postgame_turn_count(session_key)
        if postgame_turns < 2:
            system_prompt = (
                "You are Kiki, a warm and encouraging chatbot. The user just played the Stroop Effect game. "
                "Start by chatting about the game, but after a few turns, naturally transition to general friendly conversation. "
                "If the user asks about something unrelated to the game, respond naturally and don't force the conversation back to the game. "
                "You can ask about their day, hobbies, or offer to help with reminders or play another game. "
                "Avoid repeating questions about the game. If the user has already answered several questions about the game, move on to other topics or offer to help. "
                "Keep responses to 2-3 short sentences, and use playful, natural language. "
                "Inject humor occasionally with playful lines like 'You must have fruit powers 🍉 today!' or similar light-hearted expressions. "
                "Offer activity changes sometimes, such as 'Wanna try a different game or just hang out and talk?'"
                "Ask follow-up questions to keep conversations engaging."
                "Use simple, everyday English. Avoid big or complicated words. Make sure your sentences are easy to understand."
            )
        else:
            system_prompt = KIKI_SYSTEM_PROMPT
        reply = get_openai_response(user_message, session_key, system_prompt, 35, history_dict=POST_GAME_HISTORY)
        return jsonify({
            "fulfillmentText": reply,
            "outputContexts": [{
                "name": f"{session_id}/contexts/post_game_stroop",
                "lifespanCount": 3
            }]
        })

    # Continue Memory Match post-game chat
    elif intent_display_name == "ContinuePostGameChatMemory":
        session_key = f"{session_id}_memory"
        postgame_turns = get_postgame_turn_count(session_key)
        if postgame_turns < 2:
            system_prompt = (
                "You are Kiki, a warm and encouraging chatbot. The user just played the Memory Match game. "
                "Start by chatting about the game, but after a few turns, naturally transition to general friendly conversation. "
                "If the user asks about something unrelated to the game, respond naturally and don't force the conversation back to the game. "
                "You can ask about their day, hobbies, or offer to help with reminders or play another game. "
                "Avoid repeating questions about the game. If the user has already answered several questions about the game, move on to other topics or offer to help. "
                "Keep responses to 2-3 short sentences, and use playful, natural language. "
                "Inject humor occasionally with playful lines like 'You must have fruit powers 🍉 today!' or similar light-hearted expressions. "
                "Offer activity changes sometimes, such as 'Wanna try a different game or different level of Memory Match, or just hang out and talk?'"
                "Ask follow-up questions to keep conversations engaging."
                "Use simple, everyday English. Avoid big or complicated words. Make sure your sentences are easy to understand."
            )
        else:
            system_prompt = KIKI_SYSTEM_PROMPT
        reply = get_openai_response(user_message, session_key, system_prompt, 35, history_dict=POST_GAME_HISTORY)
        return jsonify({
            "fulfillmentText": reply,
            "outputContexts": [{
                "name": f"{session_id}/contexts/post_game_memory",
                "lifespanCount": 3
            }]
        })

    # Continue Stroop post-game chat
    elif intent_display_name == "ContinuePostGameChatStroop":
        session_key = f"{session_id}_stroop"
        postgame_turns = get_postgame_turn_count(session_key)
        if postgame_turns < 2:
            system_prompt = (
                "You are Kiki, a warm and encouraging chatbot. The user just played the Stroop Effect game. "
                "Start by chatting about the game, but after a few turns, naturally transition to general friendly conversation. "
                "If the user asks about something unrelated to the game, respond naturally and don't force the conversation back to the game. "
                "You can ask about their day, hobbies, or offer to help with reminders or play another game. "
                "Avoid repeating questions about the game. If the user has already answered several questions about the game, move on to other topics or offer to help. "
                "Keep responses to 2-3 short sentences, and use playful, natural language. "
                "Inject humor occasionally with playful lines like 'You must have fruit powers 🍉 today!' or similar light-hearted expressions. "
                "Offer activity changes sometimes, such as 'Wanna try a different game or just hang out and talk?'"
                "Ask follow-up questions to keep conversations engaging."
                "Use simple, everyday English. Avoid big or complicated words. Make sure your sentences are easy to understand."
            )
        else:
            system_prompt = KIKI_SYSTEM_PROMPT
        reply = get_openai_response(user_message, session_key, system_prompt, 35, history_dict=POST_GAME_HISTORY)
        return jsonify({
            "fulfillmentText": reply,
            "outputContexts": [{
                "name": f"{session_id}/contexts/post_game_stroop",
                "lifespanCount": 3
            }]
        })

    # Fallback during Memory Match post-game chat (dynamic OpenAI)
    elif intent_display_name == "FallbackDuringPostGameChatMemory":
        session_key = f"{session_id}_memory"
        postgame_turns = get_postgame_turn_count(session_key)
        if postgame_turns < 2:
            system_prompt = (
                "You are Kiki, a warm and encouraging chatbot. The user just played the Memory Match game. "
                "Start by chatting about the game, but after a few turns, naturally transition to general friendly conversation. "
                "If the user asks about something unrelated to the game, respond naturally and don't force the conversation back to the game. "
                "You can ask about their day, hobbies, or offer to help with reminders or play another game. "
                "Avoid repeating questions about the game. If the user has already answered several questions about the game, move on to other topics or offer to help. "
                "Keep responses to 1-2 short sentences, and use playful, natural language. "
                "Inject humor occasionally with playful lines like 'You must have fruit powers 🍉 today!' or similar light-hearted expressions. "
                "Offer activity changes sometimes, such as 'Wanna try a different game or different level of Memory Match, or just hang out and talk?'"
                "Ask follow-up questions to keep conversations engaging."
                "Use simple, everyday English. Avoid big or complicated words. Make sure your sentences are easy to understand."
            )
        else:
            system_prompt = KIKI_SYSTEM_PROMPT
        reply = get_openai_response(user_message, session_key, system_prompt, 35, history_dict=POST_GAME_HISTORY)
        return jsonify({
            "fulfillmentText": reply,
            "outputContexts": [{
                "name": f"{session_id}/contexts/post_game_memory",
                "lifespanCount": 2
            }]
        })

    # Fallback during Stroop Effect post-game chat (dynamic OpenAI)
    elif intent_display_name == "FallbackDuringPostGameChatStroop":
        session_key = f"{session_id}_stroop"
        postgame_turns = get_postgame_turn_count(session_key)
        if postgame_turns < 2:
            system_prompt = (
                "You are Kiki, a warm and encouraging chatbot. The user just played the Stroop Effect game. "
                "Start by chatting about the game, but after a few turns, naturally transition to general friendly conversation. "
                "If the user asks about something unrelated to the game, respond naturally and don't force the conversation back to the game. "
                "You can ask about their day, hobbies, or offer to help with reminders or play another game. "
                "Avoid repeating questions about the game. If the user has already answered several questions about the game, move on to other topics or offer to help. "
                "Keep responses to 1-2 short sentences, and use playful, natural language. "
                "Inject humor occasionally with playful lines like 'You must have fruit powers 🍉 today!' or similar light-hearted expressions. "
                "Offer activity changes sometimes, such as 'Wanna try a different game or just hang out and talk?'"
                "Ask follow-up questions to keep conversations engaging."
                "Use simple, everyday English. Avoid big or complicated words. Make sure your sentences are easy to understand."
            )
        else:
            system_prompt = KIKI_SYSTEM_PROMPT
        reply = get_openai_response(user_message, session_key, system_prompt, 35, history_dict=POST_GAME_HISTORY)
        return jsonify({
            "fulfillmentText": reply,
            "outputContexts": [{
                "name": f"{session_id}/contexts/post_game_stroop",
                "lifespanCount": 2
            }]
        })

    # Handle FallbackDuringChatIntent - Fallback only during chat mode
    elif intent_display_name == 'FallbackDuringChatIntent':
        chat_mode_active = is_chat_mode_active(req)
        print(f"FallbackDuringChatIntent - Chat mode active: {chat_mode_active}")
        print(f"User message: '{user_message}'")
        
        if chat_mode_active:
            print("Sending to OpenAI...")
            ai_response = get_openai_response(user_message, session_id, KIKI_SYSTEM_PROMPT)
            print(f"OpenAI response: {ai_response}")
            return jsonify({
                "fulfillmentText": ai_response,
                "outputContexts": [set_chat_mode_context(session_id)]
            })
        else:
            # If not in chat mode, this shouldn't be triggered, but provide a fallback
            return jsonify({
                "fulfillmentText": "I'm not sure what you mean. Would you like to set a reminder, play a game, or chat with me?",
                "fulfillmentMessages": [
                    {
                        "text": {"text": ["I'm not sure what you mean. Would you like to set a reminder, play a game, or chat with me?"]},
                        "platform": "ACTIONS_ON_GOOGLE"
                    },
                    {
                        "payload": {
                            "google": {
                                "richResponse": {
                                    "items": [
                                        {
                                            "simpleResponse": {
                                                "textToSpeech": "I'm not sure what you mean. Would you like to set a reminder, play a game, or chat with me?"
                                            }
                                        }
                                    ],
                                    "suggestions": [
                                        {"title": "Set a reminder"},
                                        {"title": "Play a game"},
                                        {"title": "Chat with me"}
                                    ]
                                }
                            }
                        }
                    }
                ]
            })
    
    # === STRUCTURED TASK HANDLING ===

    if intent_display_name == 'set.reminder':
        # Clear any lingering update contexts when starting a new reminder flow
        session_id = req['session']
        parameters = req.get('queryResult', {}).get('parameters', {})
        task = parameters.get('task')  
        if isinstance(task, list):
            task = task[0] if task else None
        if task:
            task = task.strip().lower()
        GENERIC_TASKS = {"set a reminder", "reminder", "remind me", "remind", "add reminder"}
        task = parameters.get('task')
        if isinstance(task, list):
            task = task[0] if task else None
        if task:
            task = task.strip().lower()

        if task and task in GENERIC_TASKS:
            # Clear both task and time contexts to avoid using previous values
            return jsonify({
                "fulfillmentText": "Sure! ☺️ What should I remind you about?",
                "outputContexts": [
                    {
                        "name": f"{req['session']}/contexts/await_task",
                        "lifespanCount": 2,
                        "parameters": {}
                    },
                    {
                        "name": f"{req['session']}/contexts/await_time",
                        "lifespanCount": 0,
                        "parameters": {}
                    }
                ]
            })
        date_time_str = parameters.get('date-time') 

        # Improved missing parameter handling
        if not task and not date_time_str:
            return jsonify({
                "fulfillmentText": "Sure! 😊 What should I remind you about?"
            })
        elif not task and date_time_str:
            # Format the time for user-friendly display
            try:
                dt_str = extract_datetime_str(date_time_str)
                dt_obj = datetime.fromisoformat(str(dt_str))
                user_friendly_time_str = dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
            except Exception:
                user_friendly_time_str = str(date_time_str)
            # Save time to context and ask for task
            return jsonify({
                "fulfillmentText": f"Okay! 🕒 I got the time: {user_friendly_time_str}. What should I remind you about?",
                "outputContexts": [
                    {
                        "name": f"{req['session']}/contexts/await_task",
                        "lifespanCount": 2,
                        "parameters": {
                            "date-time": date_time_str
                        }
                    }
                ]
            })
        elif not date_time_str:
            # Save task to context and ask for time
            return jsonify({
                "fulfillmentText": f"Got it — you want me to remind you to \"{task}\". 📝 When should I remind you?",
                "outputContexts": [
                    {
                        "name": f"{req['session']}/contexts/await_time",
                        "lifespanCount": 2,
                        "parameters": {
                            "task": task
                        }
                    }
                ]
            })
        else:
            try:
                print(f"Debug: Processing date_time_str: {date_time_str}")
                dt_str = extract_datetime_str(date_time_str)
                print(f"Debug: Extracted dt_str: {dt_str}")
                reminder_dt_obj = datetime.fromisoformat(str(dt_str))

                reminder_data = {
                    'task': task,
                    'remind_at': reminder_dt_obj,
                    'status': 'pending',
                    'created_at': firestore.SERVER_TIMESTAMP
                }
                db.collection('reminders').add(reminder_data)
                print(f"Reminder saved to Firestore: {reminder_data}")

                user_friendly_time_str = reminder_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")

                response = {
                    "fulfillmentMessages": [
                            {"text": {"text": [f"Got it! I'll remind you to \"{task}\" at {user_friendly_time_str}."]}}
                        ],
                        "outputContexts": [
                            {
                                "name": f"{req['session']}/contexts/await_task",
                                "lifespanCount": 0,
                                "parameters": {}
                            },
                            {
                                "name": f"{req['session']}/contexts/await_time",
                                "lifespanCount": 0,
                                "parameters": {}
                            }
                        ] + clear_all_update_contexts(session_id)
                }
                return jsonify(response)

            except ValueError as e:
                print(f"Date parsing error: {e}")
                return jsonify({
                    "fulfillmentText": "I had trouble understanding the time. Please use a clear format like 'tomorrow at 2 PM'."
                })
            except Exception as e:
                print(f"An unexpected error occurred: {e}")
                return jsonify({
                    "fulfillmentText": "I'm sorry, something went wrong while trying to set your reminder. Please try again later."
                })

    # Handle delete.reminder intent (initial request to find and confirm)
    elif intent_display_name == 'delete.reminder': 
        # Clear any lingering update contexts when starting a delete flow
        session_id = req['session']
        parameters = req.get('queryResult', {}).get('parameters', {})
        task_to_delete = parameters.get('task')
        if task_to_delete:
            task_to_delete = task_to_delete.strip().lower()
        date_time_to_delete_str = parameters.get('date-time') 

        if not task_to_delete:
            return jsonify({
                "fulfillmentText": "To delete a reminder, please tell me the task, e.g., 'delete bath reminder'."
            })

        try:
            query = db.collection('reminders') \
                      .where('task', '==', task_to_delete) \
                      .where('status', '==', 'pending') \
                      .order_by('remind_at')

            found_reminders = []

            if date_time_to_delete_str:
                # If a specific time is given, filter by time window
                try:
                    target_dt_obj = parser.isoparse(date_time_to_delete_str)
                except Exception:
                    return jsonify({
                        "fulfillmentText": "Sorry, I couldn't understand the time you gave. Please use a clear format like 'tomorrow at 2 PM'."
                    })
                time_window_start = target_dt_obj - timedelta(minutes=1)
                time_window_end = target_dt_obj + timedelta(minutes=1)
                query = query.where('remind_at', '>=', time_window_start) \
                             .where('remind_at', '<=', time_window_end)
                docs = query.limit(1).get() 
                if docs:
                    reminder_doc = next(iter(docs))
                    reminder_data = reminder_doc.to_dict()
                    reminder_id = reminder_doc.id
                    actual_user_friendly_time_str = reminder_data['remind_at'].astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                    actual_iso_time_str = reminder_data['remind_at'].isoformat()
                    session_id = req['session']
                    response = {
                        "fulfillmentText": f"I found your reminder to '{reminder_data['task']}' at {actual_user_friendly_time_str}. Do you want me to delete it?",
                        "outputContexts": [
                            {
                                "name": f"{session_id}/contexts/awaiting_deletion_confirmation",
                                "lifespanCount": 2, 
                                "parameters": {
                                    "reminder_id_to_delete": reminder_id,
                                    "reminder_task_found": reminder_data['task'],
                                    "reminder_time_found_str": actual_user_friendly_time_str,  # <-- THIS LINE
                                    "reminder_time_found_raw": actual_iso_time_str
                                }
                            }
                        ]
                    }
                    return jsonify(response)
                else:
                    user_friendly_target_time_str = target_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                    return jsonify({
                        "fulfillmentText": f"I couldn't find a pending reminder to '{task_to_delete}' around {user_friendly_target_time_str}. Please make sure the task and time are correct and it's still pending."
                    })
            else:
                docs = query.limit(5).get()
                for doc in docs:
                    data = doc.to_dict()
                    found_reminders.append({
                        'id': doc.id,
                        'task': data['task'],
                        'remind_at': data['remind_at'].astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y"),
                        'remind_at_raw': data['remind_at'].isoformat()
                    })
                if len(found_reminders) == 1:
                    reminder = found_reminders[0]
                    session_id = req['session']
                    response = {
                        "fulfillmentText": f"I found your reminder to '{reminder['task']}' at {user_friendly_time(reminder['remind_at'])}. Do you want me to delete it?",
                        "outputContexts": [
                            {
                                "name": f"{session_id}/contexts/awaiting_deletion_confirmation",
                                "lifespanCount": 2, 
                                "parameters": {
                                    "reminder_id_to_delete": reminder['id'],
                                    "reminder_task_found": reminder['task'],
                                    "reminder_time_found_str": user_friendly_time(reminder['remind_at']),
                                    "reminder_time_found_raw": reminder['remind_at_raw']
                                }
                            }
                        ]
                    }
                    return jsonify(response)
                elif len(found_reminders) > 1:
                    clarification_reminders_data = []
                    rich_content_items = []
                    for i, reminder in enumerate(found_reminders):
                        rich_content_items.append({
                            "type": "info",
                            "title": f"{i+1}. {reminder['task'].capitalize()}",
                            "subtitle": f"at {user_friendly_time(reminder['remind_at'])}",
                            "event": {
                                "name": "",
                                "languageCode": "",
                                "parameters": {}
                            }
                        })
                        clarification_reminders_data.append({
                            'id': reminder['id'],
                            'task': reminder['task'],
                            'time': user_friendly_time(reminder['remind_at']),
                            'time_raw': reminder['remind_at_raw']
                        })
                    # Add prompt as a description card
                    rich_content_items.append({
                        "type": "description",
                        "text": ["Please reply with the number, like '1' or '2'."]
                    })
                    session_id = req['session']
                    response = {
                        "fulfillmentMessages": [
                            {
                                "payload": {
                                    "richContent": [rich_content_items]
                                }
                            }
                        ],
                        "outputContexts": [
                            {
                                "name": f"{session_id}/contexts/awaiting_deletion_selection",
                                "lifespanCount": 2,
                                "parameters": {
                                    "reminders_list": json.dumps(clarification_reminders_data),
                                    "action_type": "delete"
                                }
                            }
                        ]
                    }
                    return jsonify(response)
                else:
                    return jsonify({
                        "fulfillmentText": f"I couldn't find any upcoming pending reminder to '{task_to_delete}'. Please make sure the task is correct."
            })
        except Exception as e:
            print(f"An unexpected error occurred during reminder lookup for deletion: {e}")
            return jsonify({
                "fulfillmentText": "I'm sorry, something went wrong while trying to find your reminder for deletion. Please try again later."
            })

    # Handle selection by index for deletion
    elif intent_display_name == 'select.reminder_to_manage_delete':
        parameters = req.get('queryResult', {}).get('parameters', {})
        selection_index = parameters.get('selection_index')
        session_id = req['session']
        # Get reminders list from context
        reminders_list_json = None
        for context in req.get('queryResult', {}).get('outputContexts', []):
            if 'awaiting_deletion_selection' in context.get('name', ''):
                reminders_list_json = context.get('parameters', {}).get('reminders_list')
                break
        if not reminders_list_json:
            return jsonify({
                "fulfillmentText": "I'm sorry, I don't have the list of reminders anymore. Could you please rephrase your request from the beginning?"
            })
        reminders_list = json.loads(reminders_list_json)
        # Safety check for selection_index
        try:
            selection_index = int(selection_index)
        except (ValueError, TypeError):
            return jsonify({
                "fulfillmentText": "I didn't catch which one you want to delete. Please reply with a number like 1 or 2."
            })
        if selection_index is not None and 1 <= selection_index <= len(reminders_list):
            selected_reminder = reminders_list[selection_index - 1]
            response = {
                "fulfillmentText": f"You want to delete the reminder to '{selected_reminder['task']}' at {user_friendly_time(selected_reminder['time'])}. Confirm delete it?",
                "outputContexts": [
                    {
                        "name": f"{session_id}/contexts/awaiting_deletion_confirmation",
                        "lifespanCount": 2,
                        "parameters": {
                            "reminder_id_to_delete": selected_reminder['id'],
                            "reminder_task_found": selected_reminder['task'],
                            "reminder_time_found_str": user_friendly_time(selected_reminder['time']),  # <-- THIS LINE
                            "reminder_time_found_raw": selected_reminder['time_raw']
                        }
                    }
                ]
            }
            return jsonify(response)
        else:
            return jsonify({
                "fulfillmentText": "I couldn't identify which reminder you meant. Please choose a number from the list or try specifying the time more precisely."
            })

    # Handle delete.reminder - yes intent (confirmation step)
    elif intent_display_name == 'delete.reminder - yes': 
        reminder_id_to_delete = get_context_parameter(req, 'awaiting_deletion_confirmation', 'reminder_id_to_delete')
        reminder_task_found = get_context_parameter(req, 'awaiting_deletion_confirmation', 'reminder_task_found')
        reminder_time_found_str = get_context_parameter(req, 'awaiting_deletion_confirmation', 'reminder_time_found_str')
        reminder_time_found_raw = get_context_parameter(req, 'awaiting_deletion_confirmation', 'reminder_time_found_raw')
        session_id = req['session']
        if reminder_id_to_delete:
            try:
                db.collection('reminders').document(reminder_id_to_delete).delete()
                return jsonify({
                    "fulfillmentText": f"Your reminder to '{reminder_task_found}'{f' at {reminder_time_found_str}' if reminder_time_found_str else ''} has been deleted.",
                    "outputContexts": [
                        {
                            "name": f"{session_id}/contexts/awaiting_deletion_confirmation",
                            "lifespanCount": 0
                        },
                        {
                            "name": f"{session_id}/contexts/awaiting_deletion_selection",
                            "lifespanCount": 0
                        }
                    ] + clear_all_update_contexts(session_id)
                })
            except Exception as e:
                print(f"Error deleting reminder from context: {e}")
                return jsonify({
                    "fulfillmentText": "Sorry, I couldn't delete your reminder. Please try again."
                })
        else:
            return jsonify({
                "fulfillmentText": "I'm sorry, I lost track of which reminder you wanted to delete. Please tell me again."
            })

    # Handle delete.reminder - no intent (negation of deletion confirmation)
    elif intent_display_name == 'delete.reminder - no':
        session_id = req['session']
        return jsonify({
            "fulfillmentText": "Okay, I won't delete that reminder. What else can I help you with?",
            "outputContexts": [
                {
                    "name": f"{session_id}/contexts/awaiting_deletion_confirmation",
                    "lifespanCount": 0
                },
                {
                    "name": f"{session_id}/contexts/awaiting_deletion_selection",
                    "lifespanCount": 0
                }
            ] + clear_all_update_contexts(session_id)
        })

    # Handle update.reminder intent (initial request to find and ask for confirmation)
    elif intent_display_name == 'update.reminder':
        parameters = req.get('queryResult', {}).get('parameters', {})
        task_to_update = parameters.get('task')
        old_date_time_str = extract_datetime_str(parameters.get('old-date-time'))
        new_date_time_str = extract_datetime_str(parameters.get('new-date-time'))

        # Ensure old_date_time_str and new_date_time_str are strings, not lists
        if isinstance(old_date_time_str, list):
            old_date_time_str = old_date_time_str[0] if old_date_time_str else None
        if isinstance(new_date_time_str, list):
            new_date_time_str = new_date_time_str[0] if new_date_time_str else None

        # Check for generic task names
        GENERIC_TASK_KEYWORDS = {"reminder", "reminders", "the reminder", "the reminders", "my reminder", "my reminders"}
        if task_to_update and task_to_update.lower() in GENERIC_TASK_KEYWORDS:
            session_id = req['session']
            return jsonify({
                "fulfillmentText": "Which reminder do you mean? You can say things like 'change my bath reminder' or 'change my sleep reminder'.",
                "outputContexts": clear_all_update_contexts(session_id)
            })
        
        if not task_to_update:
            # No task provided, but check if we have a time to search by
            if old_date_time_str:
                try:
                    # Search for all reminders around the specified time
                    old_dt_obj = datetime.fromisoformat(old_date_time_str)
                    time_window_start = old_dt_obj - timedelta(minutes=1)
                    time_window_end = old_dt_obj + timedelta(minutes=1)
                    
                    query = db.collection('reminders') \
                              .where('status', '==', 'pending') \
                              .where('remind_at', '>=', time_window_start) \
                              .where('remind_at', '<=', time_window_end) \
                              .order_by('remind_at')
                    
                    docs = query.limit(5).get()
                    found_reminders = []
                    
                    for doc in docs:
                        data = doc.to_dict()
                        found_reminders.append({
                            'id': doc.id,
                            'task': data['task'],
                            'remind_at': data['remind_at'].astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                        })
                    
                    if len(found_reminders) == 0:
                        user_friendly_time_str = old_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                        return jsonify({
                            "fulfillmentText": f"I couldn't find any pending reminders at {user_friendly_time_str}. Please make sure the time is correct."
                        })
                    elif len(found_reminders) == 1:
                        # Only one reminder found at that time
                        reminder = found_reminders[0]
                        session_id = req['session']
                        
                        if new_date_time_str:
                            # New time provided, proceed to confirmation
                            try:
                                new_dt_obj = datetime.fromisoformat(new_date_time_str)
                                user_friendly_new_time_str = new_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")

                                response = {
                                    "fulfillmentText": f"I found your reminder to '{reminder['task']}' at {user_friendly_time(reminder['remind_at'])}. Do you want to change it to {user_friendly_new_time_str}?",
                                    "outputContexts": [
                                        {
                                            "name": f"{session_id}/contexts/awaiting_update_confirmation",
                                            "lifespanCount": 2, 
                                            "parameters": {
                                                "reminder_id_to_update": reminder['id'],
                                                "reminder_task_found": reminder['task'],
                                                "reminder_old_time_found": user_friendly_time(reminder['remind_at']), 
                                                "reminder_new_time_desired_iso_str": new_date_time_str,
                                                "reminder_new_time_desired_formatted": user_friendly_new_time_str
                                            }
                                        }
                                    ]
                                }
                                return jsonify(response)
                            except ValueError as e:
                                print(f"Error parsing new time: {e}")
                                return jsonify({
                                    "fulfillmentText": f"I couldn't understand the new time '{new_date_time_str}'. Please try saying it like 'at 5pm today' or 'tomorrow at 8am'."
                                })
                        else:
                            # No new time provided, ask for the new time
                            response = {
                                "fulfillmentText": f"I found your reminder to '{reminder['task']}' at {user_friendly_time(reminder['remind_at'])}. Sure. What's the new time for your reminder?",
                                "outputContexts": [
                                    {
                                        "name": f"{session_id}/contexts/awaiting_update_time",
                                        "lifespanCount": 2,
                                        "parameters": {
                                            "reminder_id_to_update": reminder['id'],
                                            "reminder_task_found": reminder['task'],
                                            "reminder_old_time_found": user_friendly_time(reminder['remind_at'])
                                        }
                                    }
                                ]
                            }
                            return jsonify(response)
                    else:
                        # Multiple reminders found at that time
                        user_friendly_time_str = old_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                        clarification_reminders_data = []
                        rich_content_items = []

                        for i, reminder in enumerate(found_reminders):
                            rich_content_items.append({
                                "type": "info",
                                "title": f"{i+1}. {reminder['task'].capitalize()}",
                                "subtitle": f"at {user_friendly_time(reminder['remind_at'])}"
                            })
                            clarification_reminders_data.append({
                                'id': reminder['id'],
                                'task': reminder['task'],
                                'time': user_friendly_time(reminder['remind_at']),
                                'time_raw': reminder['remind_at']
                            })
                        # Add prompt as a description card
                        rich_content_items.append({
                            "type": "description",
                            "text": [
                                "Which one would you like to change? Please reply with a number, like '1' or '2'."
                            ]
                        })

                        session_id = req['session']
                        response = {
                            "fulfillmentMessages": [
                                {
                                    "payload": {
                                        "richContent": [rich_content_items]
                                    }
                                }
                            ],
                            "outputContexts": [
                                {
                                    "name": f"{session_id}/contexts/awaiting_update_selection",
                                    "lifespanCount": 2,
                                    "parameters": {
                                        "reminders_list": json.dumps(clarification_reminders_data),
                                        "action_type": "update_no_time" # New action type
                                    }
                                }
                            ]
                        }
                        return jsonify(response)
                        
                except ValueError as e:
                    print(f"Error parsing time: {e}")
                    return jsonify({
                        "fulfillmentText": "I had trouble understanding the time. Please use a clear format like '4pm' or '2 PM'."
                    })
                except Exception as e:
                    print(f"Unexpected error in old_date_time_str parsing: {e}")
                    return jsonify({
                        "fulfillmentText": "Sorry, something went wrong while processing your request. Please try again."
                    })
            else:
                # No task and no time provided
                session_id = req['session']
                new_date_time_str = extract_datetime_str(parameters.get('new-date-time'))
                if new_date_time_str:
                    user_friendly_new_time_str = user_friendly_time(new_date_time_str)
                    return jsonify({
                        "fulfillmentText": f"I see you want to change a reminder to {user_friendly_new_time_str}, but I need to know which reminder you want to change. Please tell me the task and the current time of the reminder you want to update. E.g., 'change sleep reminder' or 'change bath reminder'.",
                        "outputContexts": clear_all_update_contexts(session_id)
                    })
                else:
                    return jsonify({
                        "fulfillmentText": "To change a reminder, please tell me the task. E.g., 'change sleep reminder' or 'change bath reminder'.",
                        "outputContexts": clear_all_update_contexts(session_id)
            })

        try:
            # Base query for pending tasks, ordered by time
            query = db.collection('reminders') \
                      .where('task', '==', task_to_update) \
                      .where('status', '==', 'pending') \
                      .order_by('remind_at') 

            found_reminders = []
            
            if old_date_time_str:
                # If a specific old time is given, filter by time window
                try:
                    old_dt_obj = datetime.fromisoformat(old_date_time_str)
                    time_window_start = old_dt_obj - timedelta(minutes=1)
                    time_window_end = old_dt_obj + timedelta(minutes=1)
                    query = query.where('remind_at', '>=', time_window_start) \
                                 .where('remind_at', '<=', time_window_end)
                    
                    # Try to get only one if old time is specified, to match exact
                    docs = query.limit(1).get()

                    if docs:
                        reminder_doc = next(iter(docs))
                        reminder_id = reminder_doc.id
                        reminder_data = reminder_doc.to_dict()

                        # Format old and new times for user-friendly display in local timezone
                        user_friendly_old_time_str = reminder_data['remind_at'].astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                        
                        if new_date_time_str:
                            try:
                                new_dt_obj = datetime.fromisoformat(new_date_time_str)
                                user_friendly_new_time_str = new_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                                
                                session_id = req['session']
                                response = {
                                    "fulfillmentText": f"I found your reminder to '{task_to_update}' at {user_friendly_old_time_str}. Do you want to change it to {user_friendly_new_time_str}?",
                                    "outputContexts": [
                                        {
                                            "name": f"{session_id}/contexts/awaiting_update_confirmation",
                                            "lifespanCount": 2, 
                                            "parameters": {
                                                "reminder_id_to_update": reminder_id,
                                                "reminder_task_found": task_to_update,
                                                "reminder_old_time_found": user_friendly_old_time_str, 
                                                "reminder_new_time_desired_iso_str": new_date_time_str,
                                                "reminder_new_time_desired_formatted": user_friendly_new_time_str
                                            }
                                        }
                                    ]
                                }
                                print(f"Found specific reminder {reminder_id} for update. Awaiting confirmation.")
                                return jsonify(response)
                            except Exception as e:
                                print(f"Error parsing new time: {e}")
                                return jsonify({
                                    "fulfillmentText": f"I couldn't understand the new time '{new_date_time_str}'. Please try saying it like 'at 5pm today' or 'tomorrow at 8am'."
                                })
                        else:
                            # No new time provided, ask for the new time
                            session_id = req['session']
                            response = {
                                "fulfillmentText": f"I found your reminder to '{task_to_update}' at {user_friendly_old_time_str}. Sure. What's the new time for your reminder?",
                                "outputContexts": [
                                    {
                                        "name": f"{session_id}/contexts/awaiting_update_time",
                                        "lifespanCount": 2,
                                        "parameters": {
                                            "reminder_id_to_update": reminder_id,
                                            "reminder_task_found": task_to_update,
                                            "reminder_old_time_found": user_friendly_old_time_str
                                        }
                                    }
                                ]
                            }
                            return jsonify(response)
                    else:
                        user_friendly_old_time_str = old_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                        return jsonify({
                            "fulfillmentText": f"I couldn't find a pending reminder to '{task_to_update}' at {user_friendly_old_time_str}. Please make sure the task and current time are correct."
                        })
                except ValueError as e:
                    print(f"Error parsing old date time: {e}")
                    return jsonify({
                        "fulfillmentText": "I had trouble understanding the current time. Please use a clear format like '4pm' or '2 PM'."
                    })
                except Exception as e:
                    print(f"Unexpected error in old_date_time_str parsing (task-specific, branch 2): {e}")
                    return jsonify({
                        "fulfillmentText": "Sorry, something went wrong while processing your request. Please try again."
                    })
            else:
                # No specific old time provided, get all reminders for the task
                docs = query.limit(5).get() # Limit to a reasonable number of results

                for doc in docs:
                    data = doc.to_dict()
                    found_reminders.append({
                        'id': doc.id,
                        'task': data['task'],
                        'remind_at': data['remind_at'].astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                    })

            if len(found_reminders) == 0:
                # No reminders found for the task
                return jsonify({
                    "fulfillmentText": f"I couldn't find any upcoming pending reminder to '{task_to_update}'. Please make sure the task is correct."
                })
            
            elif len(found_reminders) == 1:
                # Exactly one reminder found
                reminder = found_reminders[0]
                session_id = req['session']
                
                if new_date_time_str:
                    # New time provided, proceed to confirmation
                    try:
                        new_dt_obj = datetime.fromisoformat(new_date_time_str)
                        user_friendly_new_time_str = new_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")

                        response = {
                            "fulfillmentText": f"I found your reminder to '{reminder['task']}' at {user_friendly_time(reminder['remind_at'])}. Do you want to change it to {user_friendly_new_time_str}?",
                            "outputContexts": [
                                {
                                    "name": f"{session_id}/contexts/awaiting_update_confirmation",
                                    "lifespanCount": 2, 
                                    "parameters": {
                                        "reminder_id_to_update": reminder['id'],
                                        "reminder_task_found": reminder['task'],
                                        "reminder_old_time_found": user_friendly_time(reminder['remind_at']), 
                                        "reminder_new_time_desired_iso_str": new_date_time_str,
                                        "reminder_new_time_desired_formatted": user_friendly_new_time_str
                                    }
                                }
                            ]
                        }
                        print(f"Found unique reminder {reminder['id']} for update. Awaiting confirmation.")
                        return jsonify(response)
                    except ValueError as e:
                        print(f"Error parsing new time: {e}")
                        return jsonify({
                            "fulfillmentText": f"I couldn't understand the new time '{new_date_time_str}'. Please try saying it like 'at 5pm today' or 'tomorrow at 8am'."
                        })
                else:
                    # No new time provided, ask for the new time
                    response = {
                        "fulfillmentText": f"I found your reminder to '{reminder['task']}' at {user_friendly_time(reminder['remind_at'])}. Sure. What's the new time for your reminder?",
                        "outputContexts": [
                            {
                                "name": f"{session_id}/contexts/awaiting_update_time",
                                "lifespanCount": 2,
                                "parameters": {
                                    "reminder_id_to_update": reminder['id'],
                                    "reminder_task_found": reminder['task'],
                                    "reminder_old_time_found": user_friendly_time(reminder['remind_at'])
                                }
                            }
                        ]
                    }
                    print(f"Found unique reminder {reminder['id']} for update. Asking for new time.")
                    return jsonify(response)

            elif len(found_reminders) > 1:
                # Multiple reminders found, show rich content for selection
                clarification_reminders_data = []
                rich_content_items = []

                for i, reminder in enumerate(found_reminders):
                    rich_content_items.append({
                        "type": "info",
                        "title": f"{i+1}. {reminder['task'].capitalize()}",
                        "subtitle": f"at {user_friendly_time(reminder['remind_at'])}"
                    })
                    clarification_reminders_data.append({
                        'id': reminder['id'],
                        'task': reminder['task'],
                        'time': user_friendly_time(reminder['remind_at']),
                        'time_raw': reminder['remind_at']
                    })

                # Add prompt as a description card
                rich_content_items.append({
                    "type": "description",
                    "text": [
                        "Which one would you like to change? Please reply with a number, like '1' or '2'."
                    ]
                })

                session_id = req['session']
                response = {
                    "fulfillmentMessages": [
                        {
                            "payload": {
                                "richContent": [rich_content_items]
                            }
                        }
                    ],
                    "outputContexts": [
                        {
                            "name": f"{session_id}/contexts/awaiting_update_selection",
                            "lifespanCount": 2,
                            "parameters": {
                                "reminders_list": json.dumps(clarification_reminders_data),
                                "action_type": "update_no_time" # New action type
                            }
                        }
                    ]
                }
                return jsonify(response)

        except ValueError as e:
            print(f"Date parsing error in update (should not happen if no old_date_time_str): {e}")
            return jsonify({
                "fulfillmentText": "I had trouble understanding your request. Please try again."
            })
        except Exception as e:
            print(f"An unexpected error occurred during reminder lookup for update: {e}")
            return jsonify({
                "fulfillmentText": "I'm sorry, something went wrong while trying to find your reminder for update. Please try again later."
            })

    # Handle update.reminder - yes intent (confirmation step)
    elif intent_display_name == 'update.reminder - yes':
        reminder_id_to_update = get_context_parameter(req, 'awaiting_update_confirmation', 'reminder_id_to_update')
        reminder_task_found = get_context_parameter(req, 'awaiting_update_confirmation', 'reminder_task_found')
        reminder_new_time_desired_iso_str = get_context_parameter(req, 'awaiting_update_confirmation', 'reminder_new_time_desired_iso_str')
        reminder_new_time_desired_formatted = get_context_parameter(req, 'awaiting_update_confirmation', 'reminder_new_time_desired_formatted')

        if reminder_id_to_update and reminder_new_time_desired_iso_str:
            try:
                new_dt_obj_for_update = datetime.fromisoformat(reminder_new_time_desired_iso_str)

                doc_ref = db.collection('reminders').document(reminder_id_to_update)
                doc = doc_ref.get()

                if doc.exists:
                    doc_ref.update({
                        'remind_at': new_dt_obj_for_update
                    })
                    print(f"Reminder (ID: {reminder_id_to_update}) confirmed and updated to {new_dt_obj_for_update}.")

                    session_id = req['session']
                    response = {
                        "fulfillmentText": f"Okay, I've successfully changed your reminder to '{reminder_task_found}' to {reminder_new_time_desired_formatted}.",
                        "outputContexts": clear_all_update_contexts(session_id)
                    }
                    return jsonify(response)
                else:
                    print(f"Attempted to update reminder {reminder_id_to_update} but it doesn't exist.")
                    return jsonify({
                        "fulfillmentText": "I'm sorry, I couldn't find that specific reminder to update. Please try again."
                    })

            except ValueError as e:
                print(f"Error parsing new time from context: {e}")
                return jsonify({
                    "fulfillmentText": "I had trouble confirming the new time for the reminder. Please try to change it again."
                })
            except Exception as e:
                print(f"Error updating reminder: {e}")
                return jsonify({
                    "fulfillmentText": "I'm sorry, I encountered an error while trying to update the reminder. Please try again."
                })
        else:
            print("Missing reminder ID or new time in context for update confirmation.")
            return jsonify({
                "fulfillmentText": "I'm sorry, I lost track of which reminder you wanted to update. Please tell me again."
            })

    # Handle update.reminder - no intent (negation of update confirmation)
    elif intent_display_name == 'update.reminder - no':
        session_id = req['session']
        response = {
            "fulfillmentText": "Okay, I won't change that reminder. What else can I help you with?",
            "outputContexts": clear_all_update_contexts(session_id)
        }
        print(f"User declined update. All update contexts cleared.")
        return jsonify(response)

    # Handle update.reminder_new_time for capturing new time for update
    elif intent_display_name == 'update.reminder_new_time':
        parameters = req.get('queryResult', {}).get('parameters', {})
        new_date_time = parameters.get('date-time')
        if isinstance(new_date_time, list):
            new_date_time = new_date_time[0] if new_date_time else None
        
        # Get reminder details from context
        reminder_id_to_update = get_context_parameter(req, 'awaiting_update_time', 'reminder_id_to_update')
        reminder_task_found = get_context_parameter(req, 'awaiting_update_time', 'reminder_task_found')
        reminder_old_time_found = get_context_parameter(req, 'awaiting_update_time', 'reminder_old_time_found')
        
        if not reminder_id_to_update or not new_date_time:
            return jsonify({
                "fulfillmentText": "I'm sorry, I lost track of which reminder you wanted to update. Please try again."
            })
        
        try:
            # Extract and parse the new time
            new_date_time_str = extract_datetime_str(new_date_time)
            if isinstance(new_date_time_str, list):
                new_date_time_str = new_date_time_str[0] if new_date_time_str else None
            if new_date_time_str:
                new_dt_obj = datetime.fromisoformat(new_date_time_str) # Removed redundant str() cast
                user_friendly_new_time_str = new_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
            else:
                return jsonify({
                    "fulfillmentText": f"I couldn't understand the time you gave. Please try saying it like 'at 5pm today' or 'tomorrow at 8am'."
                })
            
            session_id = req['session']
            response = {
                "fulfillmentText": f"Just to confirm: You want to change your '{reminder_task_found}' reminder from {user_friendly_time(reminder_old_time_found)} to {user_friendly_new_time_str}. Should I go ahead?",
            "outputContexts": [
                    {
                        "name": f"{session_id}/contexts/awaiting_update_time",
                        "lifespanCount": 0 # Clear the time context
                    },
                {
                    "name": f"{session_id}/contexts/awaiting_update_confirmation",
                        "lifespanCount": 2,
                        "parameters": {
                            "reminder_id_to_update": reminder_id_to_update,
                            "reminder_task_found": reminder_task_found,
                            "reminder_old_time_found": reminder_old_time_found,
                            "reminder_new_time_desired_iso_str": new_date_time_str,
                            "reminder_new_time_desired_formatted": user_friendly_new_time_str
                        }
                    }
                ]
            }
            return jsonify(response)
        except ValueError as e:
            print(f"Error parsing new time: {e}")
            return jsonify({
                "fulfillmentText": f"Sorry, I couldn't understand the time you gave. Please try saying it like 'at 5pm today' or 'tomorrow at 8am'."
            })
        except Exception as e:
            print(f"Error processing update time: {e}")
            return jsonify({
                "fulfillmentText": "I had trouble understanding the time. Please use a clear format like '5pm' or 'tomorrow at 2 PM'."
            })

        except ValueError as e:
            print(f"Error parsing new time: {e}")
            return jsonify({
                "fulfillmentText": f"Sorry, I couldn't understand the time you gave. Please try saying it like 'at 5pm today' or 'tomorrow at 8am'."
            })
        except Exception as e:
            print(f"Error processing update time: {e}")
            return jsonify({
                "fulfillmentText": "I had trouble understanding the time. Please use a clear format like '5pm' or 'tomorrow at 2 PM'."
            })

    # Handle select.reminder_to_manage_update intent (user clarifies which reminder from a list)
    elif intent_display_name == 'select.reminder_to_manage_update':
        parameters = req.get('queryResult', {}).get('parameters', {})
        selection_index = parameters.get('selection_index')
        session_id = req['session']
        # Get reminders list from context
        reminders_list_json = None
        for context in req.get('queryResult', {}).get('outputContexts', []):
            if 'awaiting_update_selection' in context.get('name', ''):
                reminders_list_json = context.get('parameters', {}).get('reminders_list')
                break
        if not reminders_list_json:
            return jsonify({
                "fulfillmentText": "I'm sorry, I don't have the list of reminders anymore. Could you please rephrase your request from the beginning?"
            })
        reminders_list = json.loads(reminders_list_json)
        # Safety check for selection_index
        try:
            selection_index = int(selection_index)
        except (ValueError, TypeError):
            return jsonify({
                "fulfillmentText": "I didn't catch which one you want to update. Please reply with a number like 1 or 2."
            })
        if selection_index is not None and 1 <= selection_index <= len(reminders_list):
            selected_reminder = reminders_list[selection_index - 1]
            response = {
                "fulfillmentText": f"You want to change the reminder to '{selected_reminder['task']}' at {user_friendly_time(selected_reminder['time'])}. What's the new time?",
                "outputContexts": [
                    {
                        "name": f"{session_id}/contexts/awaiting_update_time",
                        "lifespanCount": 2, 
                        "parameters": {
                            "reminder_id_to_update": selected_reminder['id'],
                            "reminder_task_found": selected_reminder['task'],
                            "reminder_old_time_found": user_friendly_time(selected_reminder['time'])
                        }
                    }
                ]
            }
            return jsonify(response)
        else:
            return jsonify({
                "fulfillmentText": "I couldn't identify which reminder you meant. Please choose a number from the list or try specifying the time more precisely."
            })

    
    # Handle CaptureTimeIntent for time follow-up
    elif intent_display_name == 'CaptureTimeIntent':
        parameters = req.get('queryResult', {}).get('parameters', {})
        date_time = parameters.get('date-time')

        # Get task from context
        context_list = req.get('queryResult', {}).get('outputContexts', [])
        task = None
        for context in context_list:
            if 'await_time' in context.get('name', ''):
                task = context.get('parameters', {}).get('task')
        if isinstance(task, list):
            task = task[0] if task else None
        if task:
            task = task.strip().lower()

        date_time_str = extract_datetime_str(date_time)

        if task and date_time_str and isinstance(date_time_str, str) and date_time_str.strip():
            try:
                reminder_dt_obj = datetime.fromisoformat(date_time_str)
                reminder_data = {
                    'task': task,
                    'remind_at': reminder_dt_obj,
                    'status': 'pending',
                    'created_at': firestore.SERVER_TIMESTAMP
                }
                db.collection('reminders').add(reminder_data)
                user_friendly_time_str = reminder_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                return jsonify({
                    "fulfillmentText": f"All set! I'll remind you to \"{task}\" at {user_friendly_time_str}. ✅",
                    "outputContexts": [
                        {
                            "name": f"{req['session']}/contexts/await_task",
                            "lifespanCount": 0
                        },
                        {
                            "name": f"{req['session']}/contexts/await_time",
                            "lifespanCount": 0
                        }
                    ]
                })
            except Exception as e:
                print(f"Error saving reminder in CaptureTimeIntent: {e}")
                return jsonify({
                    "fulfillmentText": "Sorry, I couldn't save your reminder. Please try again."
                })
        else:
            return jsonify({
                "fulfillmentText": "Hmm, I'm still missing some info. Could you try again?"
            })

    # Handle CaptureTaskIntent for task follow-up
    elif intent_display_name == 'CaptureTaskIntent':
        parameters = req.get('queryResult', {}).get('parameters', {})
        task = parameters.get('task')

        # Handle task as a list
        if isinstance(task, list):
            task = task[0] if task else None
        if task:
            task = task.strip().lower()

        # --- PATCH: Filter out generic tasks ---
        GENERIC_TASKS = {"set a reminder", "reminder", "remind me", "remind", "add reminder"}
        if task and task in GENERIC_TASKS:
            # Double-clear both contexts, then set a fresh await_task
            return jsonify({
                "fulfillmentText": "Sure! ☺️ What should I remind you about?",
                "outputContexts": [
                    {
                        "name": f"{req['session']}/contexts/await_task",
                        "lifespanCount": 0,
                        "parameters": {}
                        },
                        {
                        "name": f"{req['session']}/contexts/await_time",
                        "lifespanCount": 0,
                        "parameters": {}
                    },
                    {
                        "name": f"{req['session']}/contexts/await_task",
                        "lifespanCount": 2,
                        "parameters": {}
                    }
                ]
            })
        # --- END PATCH ---

        # Get time from context
        context_list = req.get('queryResult', {}).get('outputContexts', [])
        date_time = None
        for context in context_list:
            if 'await_task' in context.get('name', ''):
                date_time = context.get('parameters', {}).get('date-time')
        date_time_str = extract_datetime_str(date_time)

        # If only a task is present and no valid date-time, prompt for time and set await_time context
        if task and (not date_time_str or not (isinstance(date_time_str, str) and date_time_str.strip())):
            return jsonify({
                "fulfillmentText": f"Great! What time should I remind you to \"{task}\"?",
                "outputContexts": [
                    {
                        "name": f"{req['session']}/contexts/await_time",
                            "lifespanCount": 2, 
                            "parameters": {
                            "task": task
                        }
                    }
                ]
            })

        if task and date_time_str and isinstance(date_time_str, str) and date_time_str.strip():
            try:
                reminder_dt_obj = datetime.fromisoformat(date_time_str)
                reminder_data = {
                    'task': task,
                    'remind_at': reminder_dt_obj,
                    'status': 'pending',
                    'created_at': firestore.SERVER_TIMESTAMP
                }
                db.collection('reminders').add(reminder_data)
                user_friendly_time_str = reminder_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                return jsonify({
                    "fulfillmentText": f"All set! ✅ I'll remind you to \"{task}\" at {user_friendly_time_str}.",
                    "outputContexts": [
                        {
                            "name": f"{req['session']}/contexts/await_task",
                            "lifespanCount": 0
                        },
                        {
                            "name": f"{req['session']}/contexts/await_time",
                            "lifespanCount": 0
                        }
                    ]
                })
            except Exception as e:
                print(f"Error saving reminder in CaptureTaskIntent: {e}")
                return jsonify({
                    "fulfillmentText": "Sorry, I couldn't save your reminder. Please try again."
                })
        else:
            if task and (not date_time_str or not (isinstance(date_time_str, str) and date_time_str.strip())):
                return jsonify({
                    "fulfillmentText": f"Great! What time should I remind you to \"{task}\"?"
                })
            else:
                return jsonify({
                    "fulfillmentText": "Oops! I need both the task and the time. Could you try again?"
                })

    # Handle Default Fallback Intent - Only when not in chat mode
    elif intent_display_name == 'Default Fallback Intent':
        if is_chat_mode_active(req):
            # If in chat mode, forward to OpenAI
            ai_response = get_openai_response(user_message, session_id, KIKI_SYSTEM_PROMPT)
            return jsonify({
                "fulfillmentText": ai_response,
                "outputContexts": [set_chat_mode_context(session_id)]
            })
        else:
            # If not in chat mode, show friendly fallback with suggestions
            return jsonify({
                "fulfillmentText": "I'm not sure what you mean. Would you like to set a reminder, play a game, or chat with me?",
                "fulfillmentMessages": [
                    {
                        "text": {"text": ["I'm not sure what you mean. Would you like to set a reminder, play a game, or chat with me?"]},
                        "platform": "ACTIONS_ON_GOOGLE"
                    },
                    {
                        "payload": {
                            "google": {
                                "richResponse": {
                                    "items": [
                                        {
                                            "simpleResponse": {
                                                "textToSpeech": "I'm not sure what you mean. Would you like to set a reminder, play a game, or chat with me?"
                                            }
                                        }
                                    ],
                                    "suggestions": [
                                        {"title": "Set a reminder"},
                                        {"title": "Play a game"},
                                        {"title": "Chat with me"}
                                    ]
                                }
                            }
                        }
                    }
                ]
            })


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port, debug=True)
