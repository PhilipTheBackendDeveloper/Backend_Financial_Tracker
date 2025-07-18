"""
Finance Tracker Backend - Flask API with Firebase Integration
Professional Personal Finance Tracker
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials, firestore, auth
from datetime import datetime, timedelta
import calendar
from functools import wraps
import os
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
CORS(app, origins=["http://localhost:3000"], methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])

# Initialize Firebase Admin SDK
try:
    # Change for Render Deployment:
    cred = credentials.Certificate('/etc/secrets/serviceAccountKey.json')
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    logger.info("Firebase initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize Firebase: {e}")
    raise

# Helper Functions
def verify_firebase_token(token):
    """
    Verify Firebase ID token and return user info
    """
    try:
        # Remove 'Bearer ' prefix if present
        if token.startswith('Bearer '):
            token = token[7:]
        
        # Verify the token
        decoded_token = auth.verify_id_token(token)
        return decoded_token
    except Exception as e:
        logger.error(f"Token verification failed: {e}")
        return None

def require_auth(f):
    """
    Decorator to require authentication for protected routes
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Get token from Authorization header
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            return jsonify({'error': 'Authorization header missing'}), 401
        
        # Verify token
        decoded_token = verify_firebase_token(auth_header)
        if not decoded_token:
            return jsonify({'error': 'Invalid or expired token'}), 401
        
        # Check if user_id in URL matches token
        user_id = kwargs.get('user_id')
        if user_id and user_id != decoded_token['uid']:
            return jsonify({'error': 'Unauthorized access to user data'}), 403
        
        # Add user info to request context
        request.user = decoded_token
        return f(*args, **kwargs)
    
    return decorated_function

def get_current_month():
    """Get current month in YYYY-MM format"""
    return datetime.now().strftime('%Y-%m')

def parse_date(date_string):
    """Parse date string to datetime object"""
    try:
        return datetime.strptime(date_string, '%Y-%m-%d')
    except ValueError:
        return None

def get_month_range(month_str):
    """Get start and end dates for a given month (YYYY-MM)"""
    try:
        year, month = map(int, month_str.split('-'))
        start_date = datetime(year, month, 1)
        # Get last day of month
        last_day = calendar.monthrange(year, month)[1]
        end_date = datetime(year, month, last_day, 23, 59, 59)
        return start_date, end_date
    except ValueError:
        return None, None

# Error Handlers
@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Resource not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal server error: {error}")
    return jsonify({'error': 'Internal server error'}), 500

@app.errorhandler(Exception)
def handle_exception(e):
    logger.error(f"Unhandled exception: {e}")
    return jsonify({'error': 'An unexpected error occurred'}), 500

# Health Check Route
@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'service': 'Finance Tracker Backend',
        'version': '2.0.0'
    })

# BUDGET ROUTES
@app.route('/api/users/<user_id>/budgets', methods=['GET'])
@require_auth
def get_budgets(user_id):
    """Get budgets for current or specified month"""
    try:
        logger.info(f"Getting budgets for user: {user_id}")
        
        # Get month parameter (optional)
        month = request.args.get('month')
        logger.info(f"Month filter: {month}")
        
        # Query Firestore for budgets
        budgets_ref = db.collection('users').document(user_id).collection('budgets')
        
        if month:
            # Filter by specific month
            query = budgets_ref.where('month', '==', month).order_by('category')
        else:
            # Get all budgets if no month specified
            query = budgets_ref.order_by('month', direction=firestore.Query.DESCENDING)
        
        budgets = query.stream()
        
        # Format response
        budget_list = []
        total_budget = 0
        
        for budget in budgets:
            try:
                budget_data = budget.to_dict()
                budget_data['id'] = budget.id
                
                # Convert timestamps to ISO strings if they exist
                if 'created_at' in budget_data and budget_data['created_at']:
                    budget_data['created_at'] = budget_data['created_at'].isoformat()
                if 'updated_at' in budget_data and budget_data['updated_at']:
                    budget_data['updated_at'] = budget_data['updated_at'].isoformat()
                
                budget_list.append(budget_data)
                total_budget += budget_data.get('amount', 0)
                
            except Exception as e:
                logger.error(f"Error processing budget document: {e}")
                continue
        
        logger.info(f"Found {len(budget_list)} budgets for user {user_id}")
        
        return jsonify({
            'budgets': budget_list,
            'month': month or 'all',
            'total_budget': round(total_budget, 2),
            'count': len(budget_list)
        })
        
    except Exception as e:
        logger.error(f"Error getting budgets for user {user_id}: {str(e)}")
        return jsonify({'error': f'Failed to retrieve budgets: {str(e)}'}), 500

@app.route('/api/users/<user_id>/budgets', methods=['POST'])
@require_auth
def set_budget(user_id):
    """Set or create budget"""
    try:
        logger.info(f"Setting budget for user: {user_id}")
        
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        logger.info(f"Budget data received: {data}")
        
        # Validate required fields
        required_fields = ['amount', 'month', 'category']
        for field in required_fields:
            if field not in data or not data[field]:
                return jsonify({'error': f'Missing required field: {field}'}), 400
        
        # Validate amount
        try:
            amount = float(data['amount'])
            if amount <= 0:
                return jsonify({'error': 'Amount must be positive'}), 400
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid amount format'}), 400
        
        # Validate month format
        month = data['month'].strip()
        if not month or len(month.split('-')) != 2:
            return jsonify({'error': 'Invalid month format. Use YYYY-MM'}), 400
        
        # Get category
        category = data['category'].strip()
        if not category:
            return jsonify({'error': 'Category is required'}), 400
        
        # Check if budget already exists for this month and category
        budgets_ref = db.collection('users').document(user_id).collection('budgets')
        query = budgets_ref.where('month', '==', month).where('category', '==', category)
        existing_budgets = list(query.stream())
        
        if existing_budgets:
            return jsonify({'error': 'Budget already exists for this category and month'}), 400
        
        # Create budget document
        budget_data = {
            'amount': amount,
            'month': month,
            'category': category,
            'created_at': datetime.now(),
            'updated_at': datetime.now()
        }
        
        # Add to Firestore
        doc_ref = budgets_ref.add(budget_data)
        budget_id = doc_ref[1].id
        
        # Return created budget with ID
        budget_data['id'] = budget_id
        budget_data['created_at'] = budget_data['created_at'].isoformat()
        budget_data['updated_at'] = budget_data['updated_at'].isoformat()
        
        logger.info(f"Budget created for user {user_id}: {budget_id}")
        
        return jsonify({
            'message': 'Budget created successfully',
            'budget': budget_data
        }), 201
        
    except Exception as e:
        logger.error(f"Error setting budget: {str(e)}")
        return jsonify({'error': f'Failed to set budget: {str(e)}'}), 500

@app.route('/api/users/<user_id>/budgets/<budget_id>', methods=['PUT'])
@require_auth
def update_budget(user_id, budget_id):
    """Update existing budget"""
    try:
        logger.info(f"Updating budget {budget_id} for user: {user_id}")
        
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        # Get budget reference
        budget_ref = db.collection('users').document(user_id).collection('budgets').document(budget_id)
        budget_doc = budget_ref.get()
        
        if not budget_doc.exists:
            return jsonify({'error': 'Budget not found'}), 404
        
        # Prepare update data
        update_data = {'updated_at': datetime.now()}
        
        # Update amount if provided
        if 'amount' in data:
            try:
                amount = float(data['amount'])
                if amount <= 0:
                    return jsonify({'error': 'Amount must be positive'}), 400
                update_data['amount'] = amount
            except (ValueError, TypeError):
                return jsonify({'error': 'Invalid amount format'}), 400
        
        # Update category if provided
        if 'category' in data:
            category = data['category'].strip()
            if not category:
                return jsonify({'error': 'Category cannot be empty'}), 400
            update_data['category'] = category
        
        # Update month if provided
        if 'month' in data:
            month = data['month'].strip()
            if not month or len(month.split('-')) != 2:
                return jsonify({'error': 'Invalid month format. Use YYYY-MM'}), 400
            update_data['month'] = month
        
        # Update in Firestore
        budget_ref.update(update_data)
        
        # Get updated document
        updated_doc = budget_ref.get()
        updated_data = updated_doc.to_dict()
        updated_data['id'] = budget_id
        
        # Convert timestamps to ISO strings
        if 'created_at' in updated_data and updated_data['created_at']:
            updated_data['created_at'] = updated_data['created_at'].isoformat()
        if 'updated_at' in updated_data and updated_data['updated_at']:
            updated_data['updated_at'] = updated_data['updated_at'].isoformat()
        
        logger.info(f"Budget updated for user {user_id}: {budget_id}")
        
        return jsonify({
            'message': 'Budget updated successfully',
            'budget': updated_data
        })
        
    except Exception as e:
        logger.error(f"Error updating budget: {str(e)}")
        return jsonify({'error': f'Failed to update budget: {str(e)}'}), 500

@app.route('/api/users/<user_id>/budgets/<budget_id>', methods=['DELETE'])
@require_auth
def delete_budget(user_id, budget_id):
    """Delete budget"""
    try:
        logger.info(f"Deleting budget {budget_id} for user: {user_id}")
        
        # Get budget reference
        budget_ref = db.collection('users').document(user_id).collection('budgets').document(budget_id)
        budget_doc = budget_ref.get()
        
        if not budget_doc.exists:
            return jsonify({'error': 'Budget not found'}), 404
        
        # Delete from Firestore
        budget_ref.delete()
        
        logger.info(f"Budget deleted for user {user_id}: {budget_id}")
        
        return jsonify({'message': 'Budget deleted successfully'})
        
    except Exception as e:
        logger.error(f"Error deleting budget: {str(e)}")
        return jsonify({'error': f'Failed to delete budget: {str(e)}'}), 500

# EXPENSE ROUTES
@app.route('/api/users/<user_id>/expenses', methods=['GET'])
@require_auth
def get_expenses(user_id):
    """Get all expenses for current month or specified month"""
    try:
        # Get month parameter (default to current month)
        month = request.args.get('month', get_current_month())
        start_date, end_date = get_month_range(month)
        
        if not start_date or not end_date:
            return jsonify({'error': 'Invalid month format. Use YYYY-MM'}), 400
        
        # Query Firestore for expenses in date range
        expenses_ref = db.collection('users').document(user_id).collection('expenses')
        query = expenses_ref.where('date', '>=', start_date).where('date', '<=', end_date).order_by('date', direction=firestore.Query.DESCENDING)
        expenses = query.stream()
        
        # Format response
        expense_list = []
        for expense in expenses:
            expense_data = expense.to_dict()
            expense_data['id'] = expense.id
            # Convert timestamp to ISO string
            if 'date' in expense_data:
                expense_data['date'] = expense_data['date'].strftime('%Y-%m-%d')
            if 'created_at' in expense_data:
                expense_data['created_at'] = expense_data['created_at'].isoformat()
            if 'updated_at' in expense_data:
                expense_data['updated_at'] = expense_data['updated_at'].isoformat()
            expense_list.append(expense_data)
        
        return jsonify({
            'expenses': expense_list,
            'month': month,
            'total_count': len(expense_list)
        })
        
    except Exception as e:
        logger.error(f"Error getting expenses: {e}")
        return jsonify({'error': 'Failed to retrieve expenses'}), 500

@app.route('/api/users/<user_id>/expenses', methods=['POST'])
@require_auth
def add_expense(user_id):
    """Add new expense"""
    try:
        data = request.get_json()
        
        # Validate required fields
        required_fields = ['amount', 'category', 'date']
        for field in required_fields:
            if field not in data:
                return jsonify({'error': f'Missing required field: {field}'}), 400
        
        # Validate amount
        try:
            amount = float(data['amount'])
            if amount <= 0:
                return jsonify({'error': 'Amount must be positive'}), 400
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid amount format'}), 400
        
        # Parse and validate date
        expense_date = parse_date(data['date'])
        if not expense_date:
            return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
        
        # Create expense document
        expense_data = {
            'amount': amount,
            'category': data['category'].strip(),
            'date': expense_date,
            'note': data.get('note', '').strip(),
            'created_at': datetime.now(),
            'updated_at': datetime.now()
        }
        
        # Add to Firestore
        expenses_ref = db.collection('users').document(user_id).collection('expenses')
        doc_ref = expenses_ref.add(expense_data)
        
        # Return created expense with ID
        expense_data['id'] = doc_ref[1].id
        expense_data['date'] = expense_data['date'].strftime('%Y-%m-%d')
        expense_data['created_at'] = expense_data['created_at'].isoformat()
        expense_data['updated_at'] = expense_data['updated_at'].isoformat()
        
        logger.info(f"Expense added for user {user_id}: {amount} in {data['category']}")
        
        return jsonify({
            'message': 'Expense added successfully',
            'expense': expense_data
        }), 201
        
    except Exception as e:
        logger.error(f"Error adding expense: {e}")
        return jsonify({'error': 'Failed to add expense'}), 500

# ANALYTICS ROUTES
@app.route('/api/summary/<user_id>/<month>', methods=['GET'])
@require_auth
def get_summary(user_id, month):
    """Get financial summary for specified month"""
    try:
        start_date, end_date = get_month_range(month)
        if not start_date or not end_date:
            return jsonify({'error': 'Invalid month format. Use YYYY-MM'}), 400
        
        # Get expenses for the month
        expenses_ref = db.collection('users').document(user_id).collection('expenses')
        expenses_query = expenses_ref.where('date', '>=', start_date).where('date', '<=', end_date)
        expenses = list(expenses_query.stream())
        
        # Calculate total expenses
        total_expenses = sum(expense.to_dict().get('amount', 0) for expense in expenses)
        
        # Get budgets for the month
        budgets_ref = db.collection('users').document(user_id).collection('budgets')
        budgets_query = budgets_ref.where('month', '==', month)
        budgets = list(budgets_query.stream())
        
        # Calculate total budget
        total_budget = sum(budget.to_dict().get('amount', 0) for budget in budgets)
        
        # Calculate remaining budget and status
        remaining_budget = total_budget - total_expenses
        budget_usage_percent = (total_expenses / total_budget * 100) if total_budget > 0 else 0
        
        # Determine budget status
        if total_budget == 0:
            budget_status = 'no_budget'
        elif remaining_budget >= 0:
            budget_status = 'under_budget'
        else:
            budget_status = 'over_budget'
        
        return jsonify({
            'month': month,
            'total_expenses': round(total_expenses, 2),
            'total_budget': round(total_budget, 2),
            'remaining_budget': round(remaining_budget, 2),
            'budget_usage_percent': round(budget_usage_percent, 2),
            'budget_status': budget_status,
            'expense_count': len(expenses),
            'budget_count': len(budgets)
        })
        
    except Exception as e:
        logger.error(f"Error getting summary: {e}")
        return jsonify({'error': 'Failed to generate summary'}), 500

@app.route('/api/report/<user_id>/<month>', methods=['GET'])
@require_auth
def get_report(user_id, month):
    """Get detailed report for specified month with chart data"""
    try:
        start_date, end_date = get_month_range(month)
        if not start_date or not end_date:
            return jsonify({'error': 'Invalid month format. Use YYYY-MM'}), 400
        
        # Get expenses for the month
        expenses_ref = db.collection('users').document(user_id).collection('expenses')
        expenses_query = expenses_ref.where('date', '>=', start_date).where('date', '<=', end_date)
        expenses = list(expenses_query.stream())
        
        # Get budgets for the month
        budgets_ref = db.collection('users').document(user_id).collection('budgets')
        budgets_query = budgets_ref.where('month', '==', month)
        budgets = list(budgets_query.stream())
        
        # Create budget lookup
        budget_lookup = {}
        for budget in budgets:
            budget_data = budget.to_dict()
            category = budget_data.get('category', 'general')
            budget_lookup[category] = budget_data.get('amount', 0)
        
        # Group expenses by category
        category_expenses = {}
        total_expenses = 0
        
        for expense in expenses:
            expense_data = expense.to_dict()
            category = expense_data.get('category', 'Other')
            amount = expense_data.get('amount', 0)
            
            if category not in category_expenses:
                category_expenses[category] = {
                    'total_amount': 0,
                    'count': 0,
                    'budget': budget_lookup.get(category, 0),
                    'over_budget': False,
                    'percentage': 0
                }
            
            category_expenses[category]['total_amount'] += amount
            category_expenses[category]['count'] += 1
            total_expenses += amount
        
        # Calculate percentages and over-budget status
        top_category = None
        top_amount = 0
        over_budget_count = 0
        
        # Prepare chart data
        pie_chart_data = []
        bar_chart_data = []
        
        for category, data in category_expenses.items():
            # Calculate percentage of total expenses
            data['percentage'] = (data['total_amount'] / total_expenses * 100) if total_expenses > 0 else 0
            
            # Check if over budget
            if data['budget'] > 0 and data['total_amount'] > data['budget']:
                data['over_budget'] = True
                over_budget_count += 1
            
            # Track top spending category
            if data['total_amount'] > top_amount:
                top_amount = data['total_amount']
                top_category = category
            
            # Round amounts
            data['total_amount'] = round(data['total_amount'], 2)
            data['percentage'] = round(data['percentage'], 2)
            
            # Prepare pie chart data
            pie_chart_data.append({
                'name': category,
                'value': data['total_amount'],
                'percentage': data['percentage']
            })
            
            # Prepare bar chart data
            bar_chart_data.append({
                'category': category,
                'expenses': data['total_amount'],
                'budget': data['budget'],
                'over_budget': data['over_budget']
            })
        
        # Sort chart data by amount (descending)
        pie_chart_data.sort(key=lambda x: x['value'], reverse=True)
        bar_chart_data.sort(key=lambda x: x['expenses'], reverse=True)
        
        return jsonify({
            'month': month,
            'expenses_by_category': category_expenses,
            'top_spending_category': {
                'category': top_category,
                'amount': round(top_amount, 2)
            } if top_category else None,
            'over_budget_categories_count': over_budget_count,
            'total_expenses': round(total_expenses, 2),
            'total_categories': len(category_expenses),
            'pie_chart_data': pie_chart_data,
            'bar_chart_data': bar_chart_data
        })
        
    except Exception as e:
        logger.error(f"Error generating report: {e}")
        return jsonify({'error': 'Failed to generate report'}), 500

# Run the application
if __name__ == '__main__':
    # Check if service account key exists
    if not os.path.exists('serviceAccountKey.json'):
        logger.error("serviceAccountKey.json not found. Please add your Firebase service account key.")
        exit(1)
    
    logger.info("Starting Finance Tracker Backend v2.0.0 on http://localhost:5000")
    app.run(debug=True, host='0.0.0.0', port=5000)
