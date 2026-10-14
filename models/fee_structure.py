from odoo import models, fields


class StudentFeeType(models.Model):
    _name = 'students.fee.type'
    _description = 'Student Fee Type'

    name = fields.Char(string='Fee Type', required=True)
    amount = fields.Float(string='Amount', required=True)