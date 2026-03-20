from datetime import datetime
from app.extensions import db


class FieldInventory(db.Model):
    """Catalog of all form fields discovered across versions of a form."""
    __tablename__ = "field_inventory"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    form_id = db.Column(db.Integer, db.ForeignKey("forms.id"), nullable=False)

    # Field info
    field_name = db.Column(db.String(300), nullable=False)
    field_type = db.Column(db.String(50))     # text, checkbox, radio, signature, date, dropdown
    page_number = db.Column(db.Integer)
    is_required = db.Column(db.Boolean, default=False)

    # Change tracking across versions
    # added | removed | renamed | modified | unchanged
    change_status = db.Column(db.String(20), default="added")
    previous_name = db.Column(db.String(300))   # for renames

    # Accessibility
    has_label = db.Column(db.Boolean)
    has_tooltip = db.Column(db.Boolean)
    tab_order = db.Column(db.Integer)

    scanned_at = db.Column(db.DateTime, default=datetime.utcnow)

    form = db.relationship("Form", backref=db.backref("field_inventory", lazy="dynamic"))
    project = db.relationship("Project", backref=db.backref("field_inventory", lazy="dynamic"))

    def __repr__(self):
        return f"<FieldInventory id={self.id} form={self.form_id} field={self.field_name}>"
