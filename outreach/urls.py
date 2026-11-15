from django.urls import path

from outreach import views

app_name = "outreach"

urlpatterns = [
    path("", views.field_capture_view, name="field_capture"),
    path("field/handshake/<int:pk>/", views.field_qr_handshake_view, name="field_qr_handshake"),
    path("dashboard/", views.dashboard_view, name="dashboard"),
    path("contacts/<int:pk>/mark-manual-sent/", views.mark_manual_sent_view, name="mark_manual_sent"),
    path("contacts/<int:pk>/reply/", views.coordinator_reply_view, name="coordinator_reply"),
    path("contacts/<int:pk>/note/", views.add_note_view, name="add_note"),
    path("contacts/<int:pk>/trigger-step/", views.trigger_step_now_view, name="trigger_step_now"),
    path("contacts/<int:pk>/opt-out/", views.toggle_opt_out_view, name="toggle_opt_out"),
    path("dashboard/export-rsvps/", views.export_rsvps_csv_view, name="export_rsvps_csv"),
    path("import/", views.bulk_import_view, name="bulk_import"),
    path("import/sample-csv/", views.sample_csv_view, name="sample_csv"),
    path("r/<str:rsvp_token>/", views.rsvp_card_view, name="rsvp_card"),
    path("webhooks/whatsapp/", views.whatsapp_webhook_view, name="whatsapp_webhook"),
    path("healthz/", views.healthcheck_view, name="healthcheck"),
]
