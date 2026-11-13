"""URL routes for expenses under /api/v1/."""
from django.urls import path

from .views import ExpenseCancelView, ExpenseDecideView, ExpenseDetailView, ExpenseListCreateView

urlpatterns = [
    path("expenses", ExpenseListCreateView.as_view(), name="expenses-list-create"),
    path("expenses/<int:expense_id>", ExpenseDetailView.as_view(), name="expense-detail"),
    path("expenses/<int:expense_id>/decide", ExpenseDecideView.as_view(), name="expense-decide"),
    path("expenses/<int:expense_id>/cancel", ExpenseCancelView.as_view(), name="expense-cancel"),
]
