from django.urls import path

from .views import LoginView, MyAccountsView, MyLedgerView, RegisterView

urlpatterns = [
    path("auth/register", RegisterView.as_view(), name="register"),
    path("auth/login", LoginView.as_view(), name="login"),
    path("accounts/me", MyAccountsView.as_view(), name="my-accounts"),
    path("accounts/me/ledger", MyLedgerView.as_view(), name="my-ledger"),
]
