# from odoo import http


# class StudentsManagement(http.Controller):
#     @http.route('/students_management/students_management', auth='public')
#     def index(self, **kw):
#         return "Hello, world"

#     @http.route('/students_management/students_management/objects', auth='public')
#     def list(self, **kw):
#         return http.request.render('students_management.listing', {
#             'root': '/students_management/students_management',
#             'objects': http.request.env['students_management.students_management'].search([]),
#         })

#     @http.route('/students_management/students_management/objects/<model("students_management.students_management"):obj>', auth='public')
#     def object(self, obj, **kw):
#         return http.request.render('students_management.object', {
#             'object': obj
#         })

