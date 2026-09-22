from django.urls import path

from apps.accounts.views import BalanceListView, LoginView, RegisterView

urlpatterns = [
    path("auth/register/", RegisterView.as_view(), name="register"),
    path("auth/login/", LoginView.as_view(), name="login"),
    path("balances/", BalanceListView.as_view(), name="balance-list"),
]
