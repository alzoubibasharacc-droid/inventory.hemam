document.addEventListener('DOMContentLoaded', function () {

    // Auto-dismiss alerts after 4 seconds
    document.querySelectorAll('.alert.alert-dismissible').forEach(function (alert) {
        setTimeout(function () {
            var bsAlert = bootstrap.Alert.getOrCreateInstance(alert);
            bsAlert.close();
        }, 4000);
    });

    // Highlight row when count input is focused
    document.querySelectorAll('.count-input').forEach(function (input) {
        input.addEventListener('focus', function () {
            this.closest('tr').classList.add('table-active');
        });
        input.addEventListener('blur', function () {
            this.closest('tr').classList.remove('table-active');
        });
    });

    // Number-only inputs
    document.querySelectorAll('input[data-numeric]').forEach(function (input) {
        input.addEventListener('input', function () {
            this.value = this.value.replace(/[^0-9.]/g, '');
        });
    });

    // Confirm delete actions
    document.querySelectorAll('[data-confirm]').forEach(function (el) {
        el.addEventListener('click', function (e) {
            if (!confirm(this.dataset.confirm)) {
                e.preventDefault();
            }
        });
    });

    // Unit converter widget
    var fromSelect = document.getElementById('conv-from');
    var toSelect = document.getElementById('conv-to');
    var convInput = document.getElementById('conv-value');
    var convResult = document.getElementById('conv-result');

    if (fromSelect && toSelect && convInput) {
        function doConvert() {
            var val = parseFloat(convInput.value);
            if (isNaN(val)) { convResult.textContent = '-'; return; }
            fetch('/inventory/convert?from=' + fromSelect.value + '&to=' + toSelect.value + '&value=' + val)
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    convResult.textContent = data.result !== null
                        ? data.result.toFixed(3) + ' ' + data.to_unit
                        : 'لا يوجد تحويل';
                });
        }
        [fromSelect, toSelect, convInput].forEach(function (el) {
            el.addEventListener('change', doConvert);
            el.addEventListener('input', doConvert);
        });
    }

});
