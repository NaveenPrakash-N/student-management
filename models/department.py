from odoo import models, fields


class StudentClass(models.Model):
    _name = 'students.department'
    _description = 'Student Deptartment'
    name = fields.Char(string='Department')