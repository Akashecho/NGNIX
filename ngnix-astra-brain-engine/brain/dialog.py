from dataclasses import dataclass, field
import secrets


FLOWS = {
    'eligibility': [
        ('state', 'Which state and district are you in?'),
        ('scheme', 'Which scheme do you want to understand?'),
        ('cooperative_type', 'Is your cooperative state-registered, multi-state, or are you unsure?'),
        ('question', 'What eligibility requirement would you like explained? Do not include identity or account numbers.'),
    ],
    'grievance': [
        ('state', 'Which state is the cooperative in?'),
        ('registration', 'Is it state-registered, multi-state, or are you unsure?'),
        ('issue', 'Briefly describe the service problem without personal or financial identifiers.'),
        ('steps_taken', 'Have you contacted the society already, and what response did you receive?'),
    ],
    'financial_literacy': [
        ('topic', 'Would you like help understanding loan costs, budgeting, saving, or another financial concept?'),
        ('question', 'What would you like explained? Please avoid account numbers and other personal identifiers.'),
    ],
}


@dataclass
class Dialog:
    name: str
    reference: str = field(default_factory=lambda: 'LOCAL-' + secrets.token_hex(6).upper())
    values: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.name not in FLOWS:
            raise ValueError('Unknown guided dialog')

    def snapshot(self):
        index = len(self.values)
        fields = FLOWS[self.name]
        return {'flow': self.name, 'reference': self.reference, 'reference_scope': 'local_draft_only',
            'submitted': False, 'complete': index == len(fields),
            'next_field': fields[index][0] if index < len(fields) else None,
            'question': fields[index][1] if index < len(fields) else None,
            'values': dict(self.values), 'ui_language': 'en'}

    def answer(self, key, value):
        state = self.snapshot()
        if state['complete'] or key != state['next_field'] or not value.strip() or len(value) > 1000:
            raise ValueError('Answer the current dialog field with 1–1000 characters')
        self.values[key] = value.strip()
        return self.snapshot()

    def query(self):
        if not self.snapshot()['complete']:
            raise ValueError('Dialog is incomplete')
        return (f'Explain the relevant official guidance for {self.name}. This is not an eligibility decision or a submitted grievance. '
                + ' '.join(f'{key}: {value}.' for key, value in self.values.items()))
