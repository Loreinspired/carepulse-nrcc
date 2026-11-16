from django import forms
from django.conf import settings

from outreach.models import OutreachContact
from outreach.utils import PhoneNormalizationError, normalize_nigeria_phone


class FieldCaptureForm(forms.Form):
    """15-second mobile capture form for soul-winners in Ado-Ekiti."""

    first_name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(
            attrs={
                "placeholder": "e.g., Tunde",
                "autocomplete": "given-name",
                "autofocus": "autofocus",
            }
        ),
    )
    last_name = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(
            attrs={
                "placeholder": "Optional surname",
                "autocomplete": "family-name",
            }
        ),
    )
    phone = forms.CharField(
        max_length=32,
        widget=forms.TextInput(
            attrs={
                "placeholder": "e.g., 0803 123 4567",
                "inputmode": "tel",
                "autocomplete": "tel",
            }
        ),
    )
    outreach_outcome = forms.ChoiceField(
        choices=OutreachContact.OutreachOutcome.choices,
        initial=OutreachContact.OutreachOutcome.INVITED,
        required=False,
        widget=forms.RadioSelect,
    )
    location_met = forms.CharField(
        max_length=150,
        required=False,
        widget=forms.TextInput(
            attrs={
                "placeholder": "e.g., Fajuyi Park, Oja Oba, EKSU Gate",
                "list": "ado-ekiti-locations",
            }
        ),
    )
    soul_winner_name = forms.CharField(
        max_length=120,
        required=False,
        widget=forms.TextInput(
            attrs={
                "placeholder": "Your name (remembered on this phone)",
            }
        ),
    )

    def clean_phone(self) -> str:
        raw = self.cleaned_data["phone"]
        try:
            return normalize_nigeria_phone(raw)
        except PhoneNormalizationError as exc:
            raise forms.ValidationError(str(exc)) from exc


class BulkImportForm(forms.Form):
    """CSV upload & hourly stagger configuration for up to 5,000+ contacts."""

    csv_file = forms.FileField(
        help_text="UTF-8 CSV with columns: first_name, last_name, phone, location_met, soul_winner_name"
    )
    max_per_hour = forms.IntegerField(
        min_value=1,
        max_value=5000,
        initial=lambda: settings.WA_MAX_PER_HOUR,
        help_text="Contacts scheduled per hour (with randomized jitter inside each hour).",
    )
    default_location = forms.CharField(
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Fallback location (e.g., Ado-Ekiti Outreach)"}),
    )
    default_soul_winner = forms.CharField(
        max_length=120,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Fallback team / coordinator name"}),
    )


class RsvpForm(forms.Form):
    """Public 1-tap Sunday RSVP micro-page form."""

    rsvp_status = forms.ChoiceField(
        choices=[
            (OutreachContact.RsvpStatus.ATTENDING, "Yes, I'm Coming This Sunday"),
            (OutreachContact.RsvpStatus.NEED_RIDE, "Yes, Coming — I Need a Free Ride / Bus"),
            (OutreachContact.RsvpStatus.MAYBE, "Might Make It — Keep Me Updated"),
            (OutreachContact.RsvpStatus.PRAYER_ONLY, "Can't Attend Sunday — Please Keep Praying"),
        ],
        widget=forms.RadioSelect,
    )
    rsvp_pickup_landmark = forms.CharField(
        max_length=150,
        required=False,
        widget=forms.TextInput(
            attrs={"placeholder": "Closest bus stop (e.g., Fajuyi, EKSU Gate, Oja Oba)"}
        ),
    )
