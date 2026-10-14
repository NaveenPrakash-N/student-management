from odoo import models, fields


class StudentSubject(models.Model):
    _name = 'students.subject'
    _description = 'Student Subject'
    name = fields.Char(string='Subject Name')