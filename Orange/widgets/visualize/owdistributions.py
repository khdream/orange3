from functools import partial
from itertools import count, groupby, repeat
from xml.sax.saxutils import escape

import numpy as np
from scipy.stats import norm, rayleigh, beta, gamma, pareto, expon

from AnyQt.QtWidgets import QGraphicsRectItem
from AnyQt.QtGui import QColor, QPen, QBrush, QPainter, QPalette, QPolygonF
from AnyQt.QtCore import Qt, QRectF, QPointF, pyqtSignal as Signal
import pyqtgraph as pg

from Orange.data import Table, DiscreteVariable, ContinuousVariable
from Orange.preprocess.discretize import decimal_binnings
from Orange.statistics import distribution, contingency
from Orange.widgets import gui, settings
from Orange.widgets.utils.annotated_data import \
    create_groups_table, create_annotated_table, ANNOTATED_DATA_SIGNAL_NAME
from Orange.widgets.utils.itemmodels import DomainModel
from Orange.widgets.utils.widgetpreview import WidgetPreview
from Orange.widgets.visualize.utils.plotutils import ElidedLabelsAxis
from Orange.widgets.widget import Input, Output, OWWidget, Msg

from Orange.widgets.visualize.owscatterplotgraph import \
    LegendItem as SPGLegendItem


class ScatterPlotItem(pg.ScatterPlotItem):
    Symbols = pg.graphicsItems.ScatterPlotItem.Symbols

    # pylint: disable=arguments-differ
    def paint(self, painter, option, widget=None):
        if self.opts["pxMode"]:
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        if self.opts["antialias"]:
            painter.setRenderHint(QPainter.Antialiasing, True)
        super().paint(painter, option, widget)


class LegendItem(SPGLegendItem):
    @staticmethod
    def mousePressEvent(event):
        if event.button() == Qt.LeftButton:
            event.accept()
        else:
            event.ignore()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            event.accept()
            if self.parentItem() is not None:
                self.autoAnchor(
                    self.pos() + (event.pos() - event.lastPos()) / 2)
        else:
            event.ignore()

    @staticmethod
    def mouseReleaseEvent(event):
        if event.button() == Qt.LeftButton:
            event.accept()
        else:
            event.ignore()


class DistributionBarItem(pg.GraphicsObject):
    def __init__(self, x, width, padding, freqs, colors, stacked, expanded,
                 tooltip):
        super().__init__()
        self.x = x
        self.width = width
        self.freqs = freqs
        self.colors = colors
        self.padding = padding
        self.stacked = stacked
        self.expanded = expanded
        self.__picture = None
        self.polygon = None
        self.hovered = False
        self.setAcceptHoverEvents(True)
        self.setToolTip(tooltip)

    def hoverEnterEvent(self, event):
        super().hoverEnterEvent(event)
        self.hovered = True
        self.update()

    def hoverLeaveEvent(self, event):
        super().hoverLeaveEvent(event)
        self.hovered = False
        self.update()

    def paint(self, painter, _options, _widget):
        if self.expanded:
            tot = np.sum(self.freqs)
            if tot == 0:
                return
            freqs = self.freqs / tot
        else:
            freqs = self.freqs

        if not self.padding:
            padding = self.mapRectFromDevice(QRectF(0, 0, 0.5, 0)).width()
        else:
            padding = min(20, self.width * self.padding)
        sx = self.x + padding
        padded_width = self.width - 2 * padding

        if self.stacked:
            painter.setPen(Qt.NoPen)
            y = 0
            for freq, color in zip(freqs, self.colors):
                painter.setBrush(QBrush(color))
                painter.drawRect(QRectF(sx, y, padded_width, freq))
                y += freq
            self.polygon = QPolygonF(QRectF(sx, 0, padded_width, y))
        else:
            polypoints = [QPointF(sx, 0)]
            pen = QPen(QBrush(Qt.white), 0.5)
            pen.setCosmetic(True)
            painter.setPen(pen)
            wsingle = padded_width / len(self.freqs)
            for i, freq, color in zip(count(), freqs, self.colors):
                painter.setBrush(QBrush(color))
                x = sx + wsingle * i
                painter.drawRect(
                    QRectF(x, 0, wsingle, freq))
                polypoints += [QPointF(x, freq),
                               QPointF(x + wsingle, freq)]
            polypoints += [QPointF(polypoints[-1].x(), 0), QPointF(sx, 0)]
            self.polygon = QPolygonF(polypoints)

        if self.hovered:
            pen = QPen(QBrush(Qt.blue), 2, Qt.DashLine)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPolygon(self.polygon)

    @property
    def x0(self):
        return self.x

    @property
    def x1(self):
        return self.x + self.width

    def boundingRect(self):
        if self.expanded:
            height = 1
        elif self.stacked:
            height = sum(self.freqs)
        else:
            height = max(self.freqs)
        return QRectF(self.x, 0, self.width, height)


class DistributionWidget(pg.PlotWidget):
    item_clicked = Signal(DistributionBarItem, Qt.KeyboardModifiers, bool)
    blank_clicked = Signal()
    mouse_released = Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_item = None

    def _get_bar_item(self, pos):
        for item in self.items(pos):
            if isinstance(item, DistributionBarItem):
                return item
        return None

    def mousePressEvent(self, ev):
        super().mousePressEvent(ev)
        if ev.button() != Qt.LeftButton:
            ev.ignore()
            return

        ev.accept()
        self.last_item = self._get_bar_item(ev.pos())
        if self.last_item:
            self.item_clicked.emit(self.last_item, ev.modifiers(), False)
        else:
            self.blank_clicked.emit()

    def mouseReleaseEvent(self, ev):
        self.last_item = None
        self.mouse_released.emit()

    def mouseMoveEvent(self, ev):
        super().mouseMoveEvent(ev)
        if self.last_item is not None:
            item = self._get_bar_item(ev.pos())
            if item is not None and item is not self.last_item:
                self.item_clicked.emit(item, ev.modifiers(), True)
                self.last_item = item


class OWDistributions(OWWidget):
    name = "Distributions"
    description = "Display value distributions of a data feature in a graph."
    icon = "icons/Distribution.svg"
    priority = 120
    keywords = []

    class Inputs:
        data = Input("Data", Table, doc="Set the input dataset")

    class Outputs:
        selected_data = Output("Selected Data", Table, default=True)
        annotated_data = Output(ANNOTATED_DATA_SIGNAL_NAME, Table)
        histogram_data = Output("Histogram Data", Table)

    class Error(OWWidget.Error):
        no_defined_values_var = \
            Msg("Variable '{}' does not have any defined values")
        no_defined_values_pair = \
            Msg("No data instances with '{}' and '{}' defined")

    class Warning(OWWidget.Warning):
        ignored_nans = Msg("Data instances with missing values are ignored")

    settingsHandler = settings.DomainContextHandler()
    var = settings.ContextSetting(None)
    cvar = settings.ContextSetting(None)
    selection = settings.ContextSetting(set(), schema_only=True)
    # number_of_bins must be a context setting because selection depends on it
    number_of_bins = settings.ContextSetting(5, schema_only=True)

    fitted_distribution = settings.Setting(0)
    show_probs = settings.Setting(False)
    stacked_columns = settings.Setting(False)
    cumulative_distr = settings.Setting(False)

    auto_apply = settings.Setting(True)

    graph_name = "plot"

    Fitters = (
        ("None", None, (), ()),
        ("Normal", norm, ("loc", "scale"), ("μ", "σ²")),
        ("Beta", beta, ("a", "b", "loc", "scale"),
         ("α", "β", "-loc", "-scale")),
        ("Gamma", gamma, ("a", "loc", "scale"), ("α", "β", "-loc", "-scale")),
        ("Rayleigh", rayleigh, ("loc", "scale"), ("-loc", "σ²")),
        ("Pareto", pareto, ("b", "loc", "scale"), ("α", "-loc", "-scale")),
        ("Exponential", expon, ("loc", "scale"), ("-loc", "λ")),
    )

    DragNone, DragAdd, DragRemove = range(3)

    def __init__(self):
        super().__init__()
        self.data = None
        self.valid_data = self.valid_group_data = None
        self.bar_items = []
        self.curve_descriptions = None
        self.binnings = []

        self.last_click_idx = None
        self.drag_operation = self.DragNone
        self.key_operation = None
        self._user_var_bins = {}

        gui.listView(
            self.controlArea, self, "var", box="Variable",
            model=DomainModel(valid_types=DomainModel.PRIMITIVE,
                              separators=False),
            callback=self._on_var_changed)

        box = self.continuous_box = gui.vBox(self.controlArea, "Distribution")
        slider = gui.hSlider(
            box, self, "number_of_bins",
            label="Bin width", orientation=Qt.Horizontal,
            minValue=0, maxValue=max(1, len(self.binnings) - 1),
            createLabel=False, callback=self._on_bins_changed)
        self.bin_width_label = gui.widgetLabel(slider.box)
        self.bin_width_label.setFixedWidth(35)
        self.bin_width_label.setAlignment(Qt.AlignRight)
        slider.sliderReleased.connect(self._on_bin_slider_released)
        gui.comboBox(
            box, self, "fitted_distribution", label="Fitted distribution",
            orientation=Qt.Horizontal, items=(name[0] for name in self.Fitters),
            callback=self.replot)
        gui.checkBox(
            box, self, "cumulative_distr", "Show cumulative distribution",
            callback=self.replot)

        box = gui.vBox(self.controlArea, "Columns")
        gui.comboBox(
            box, self, "cvar", label="Split by", orientation=Qt.Horizontal,
            model=DomainModel(placeholder="(None)",
                              valid_types=(DiscreteVariable), ),
            callback=self._on_cvar_changed, contentsLength=18)
        box2 = gui.indentedBox(box, sep=12)
        gui.checkBox(
            box2, self, "stacked_columns", "Stack columns",
            callback=self.replot)
        gui.checkBox(
            box2, self, "show_probs", "Show probabilities",
            callback=self._on_show_probabilities_changed)

        gui.auto_commit(
            self.controlArea, self, "auto_apply", "&Apply", commit=self.apply)

        self._setup_plots()
        self._setup_legend()

    def _setup_plots(self):
        def add_new_plot(zvalue):
            plot = pg.ViewBox(enableMouse=False, enableMenu=False)
            self.ploti.scene().addItem(plot)
            pg.AxisItem("right").linkToView(plot)
            plot.setXLink(self.ploti)
            plot.setZValue(zvalue)
            return plot

        self.plotview = DistributionWidget(background=None)
        self.plotview.item_clicked.connect(self._on_item_clicked)
        self.plotview.blank_clicked.connect(self._on_blank_clicked)
        self.plotview.mouse_released.connect(self._on_end_selecting)
        self.plotview.setRenderHint(QPainter.Antialiasing)
        self.mainArea.layout().addWidget(self.plotview)
        self.ploti = pg.PlotItem(
            enableMenu=False, enableMouse=False,
            axisItems={"bottom": ElidedLabelsAxis("bottom")})
        self.plot = self.ploti.vb
        self.plot.setMouseEnabled(False, False)
        self.ploti.hideButtons()
        self.plotview.setCentralItem(self.ploti)

        self.plot_pdf = add_new_plot(10)
        self.plot_mark = add_new_plot(-10)
        self.plot_mark.setYRange(0, 1)
        self.ploti.vb.sigResized.connect(self.update_views)
        self.update_views()

        pen = QPen(self.palette().color(QPalette.Text))
        self.ploti.getAxis("bottom").setPen(pen)
        left = self.ploti.getAxis("left")
        left.setPen(pen)
        left.setStyle(stopAxisAtTick=(True, True))

    def _setup_legend(self):
        self._legend = LegendItem()
        self._legend.setParentItem(self.plot)
        self._legend.hide()
        self._legend.anchor((1, 0), (1, 0))

    # -----------------------------
    # Event and signal handlers

    def update_views(self):
        for plot in (self.plot_pdf, self.plot_mark):
            plot.setGeometry(self.plot.sceneBoundingRect())
            plot.linkedViewChanged(self.plot, plot.XAxis)

    def onDeleteWidget(self):
        self.plot.clear()
        self.plot_pdf.clear()
        self.plot_mark.clear()
        super().onDeleteWidget()

    @Inputs.data
    def set_data(self, data):
        self.closeContext()
        self.var = self.cvar = None
        self.data = data
        domain = self.data.domain if self.data else None
        varmodel = self.controls.var.model()
        cvarmodel = self.controls.cvar.model()
        varmodel.set_domain(domain)
        cvarmodel.set_domain(domain)
        if varmodel:
            self.var = varmodel[min(len(domain.class_vars), len(varmodel) - 1)]
        if domain is not None and domain.has_discrete_class:
            self.cvar = domain.class_var
        self.reset_select()
        self._user_var_bins.clear()
        self.openContext(domain)
        self.set_valid_data()
        self.recompute_binnings()
        self.replot()
        self.apply()

    def _on_var_changed(self):
        self.reset_select()
        self.set_valid_data()
        self.recompute_binnings()
        self.replot()
        self.apply()

    def _on_cvar_changed(self):
        self.set_valid_data()
        self.replot()
        self.apply()

    def _on_bins_changed(self):
        self.reset_select()
        self._set_bin_width_slider_label()
        self.replot()
        # this is triggered when dragging, so don't call apply here;
        # apply is called on sliderReleased

    def _on_bin_slider_released(self):
        self._user_var_bins[self.var] = self.number_of_bins
        self.apply()

    def _set_bin_width_slider_label(self):
        text = ""
        if self.number_of_bins < len(self.binnings):
            bins = self.binnings[self.number_of_bins]
            if len(bins) > 1:
                text = self.var.repr_val(bins[1] - bins[0])
        self.bin_width_label.setText(text)

    def _on_show_probabilities_changed(self):
        label = self.controls.fitted_distribution.label
        if self.show_probs:
            label.setText("Fitted probability")
            label.setToolTip(
                "Chosen distribution is used to compute Bayesian probabilities")
        else:
            label.setText("Fitted distribution")
            label.setToolTip("")
        self.replot()

    @property
    def is_valid(self):
        return self.valid_data is not None

    def set_valid_data(self):
        err_def_var = self.Error.no_defined_values_var
        err_def_pair = self.Error.no_defined_values_pair
        err_def_var.clear()
        err_def_pair.clear()
        self.Warning.ignored_nans.clear()

        self.valid_data = self.valid_group_data = None
        if self.var is None:
            return

        column = self.data.get_column_view(self.var)[0].astype(float)
        valid_mask = np.isfinite(column)
        if not np.any(valid_mask):
            self.Error.no_defined_values_var(self.var.name)
            return
        if self.cvar:
            ccolumn = self.data.get_column_view(self.cvar)[0].astype(float)
            valid_mask *= np.isfinite(ccolumn)
            if not np.any(valid_mask):
                self.Error.no_defined_values_pair(self.var.name, self.cvar.name)
                return
            self.valid_group_data = ccolumn[valid_mask]
        if not np.all(valid_mask):
            self.Warning.ignored_nans()
        self.valid_data = column[valid_mask]

    # -----------------------------
    # Plotting

    def replot(self):
        self._clear_plot()
        if self.is_valid:
            self._set_axis_names()
            self._update_controls_state()
            self._call_plotting()
            self._display_legend()
        self.show_selection()

    def _clear_plot(self):
        self.plot.clear()
        self.plot_pdf.clear()
        self.plot_mark.clear()
        self.bar_items = []
        self._legend.clear()
        self._legend.hide()

    def _set_axis_names(self):
        assert self.is_valid  # called only from replot, so assumes data is OK
        bottomaxis = self.ploti.getAxis("bottom")
        bottomaxis.setLabel(self.var and self.var.name)

        leftaxis = self.ploti.getAxis("left")
        if self.show_probs and self.cvar:
            leftaxis.setLabel(
                f"Probability of '{self.cvar.name}' at given '{self.var.name}'")
        else:
            leftaxis.setLabel("Frequency")
        leftaxis.resizeEvent()

    def _update_controls_state(self):
        assert self.is_valid  # called only from replot, so assumes data is OK
        self.continuous_box.setDisabled(self.var.is_discrete)
        self.controls.show_probs.setDisabled(self.cvar is None)
        self.controls.stacked_columns.setDisabled(self.cvar is None)

    def _call_plotting(self):
        assert self.is_valid  # called only from replot, so assumes data is OK
        self.curve_descriptions = None
        if self.var.is_discrete:
            if self.cvar:
                self._disc_split_plot()
            else:
                self._disc_plot()
        else:
            if self.cvar:
                self._cont_split_plot()
            else:
                self._cont_plot()
        self.plot.autoRange()

    def _add_bar(self, x, width, padding, freqs, colors, stacked, expanded,
                 tooltip):
        item = DistributionBarItem(
            x, width, padding, freqs, colors, stacked, expanded, tooltip)
        self.plot.addItem(item)
        self.bar_items.append(item)

    def _disc_plot(self):
        var = self.var
        self.ploti.getAxis("bottom").setTicks([list(enumerate(var.values))])
        colors = [QColor(0, 128, 255)]
        dist = distribution.get_distribution(self.data, self.var)
        for i, freq in enumerate(dist):
            tooltip = \
                "<p style='white-space:pre;'>" \
                f"<b>{escape(var.values[i])}</b>: {int(freq)} " \
                f"({100 * freq / len(self.valid_data):.2f} %) "
            self._add_bar(
                i - 0.5, 1, 0.1, [freq], colors,
                stacked=False, expanded=False, tooltip=tooltip)

    def _disc_split_plot(self):
        var = self.var
        self.ploti.getAxis("bottom").setTicks([list(enumerate(var.values))])
        gcolors = [QColor(*col) for col in self.cvar.colors]
        gvalues = self.cvar.values
        conts = contingency.get_contingency(self.data, self.cvar, self.var)
        total = len(self.data)
        for i, freqs in enumerate(conts):
            self._add_bar(
                i - 0.5, 1, 0.1, freqs, gcolors,
                stacked=self.stacked_columns, expanded=self.show_probs,
                tooltip=self._split_tooltip(
                    var.values[i], np.sum(freqs), total, gvalues, freqs))

    def _cont_plot(self):
        self.ploti.getAxis("bottom").setTicks(None)
        data = self.valid_data
        y, x = np.histogram(data, bins=self.binnings[self.number_of_bins])
        total = len(data)
        colors = [QColor(0, 128, 255)]
        if self.fitted_distribution:
            colors[0] = colors[0].lighter(130)

        tot_freq = 0
        lasti = len(y) - 1
        for i, (x0, x1), freq in zip(count(), zip(x, x[1:]), y):
            tot_freq += freq
            tooltip = \
                "<p style='white-space:pre;'>" \
                f"<b>{escape(self.str_int(x0, x1, not i, i == lasti))}</b>: " \
                f"{freq} ({100 * freq / total:.2f} %)</p>"
            self._add_bar(
                x0, x1 - x0, 0, [tot_freq if self.cumulative_distr else freq],
                colors, stacked=False, expanded=False, tooltip=tooltip)

        if self.fitted_distribution:
            self._plot_approximations(
                x[0], x[-1], [self._fit_approximation(data)],
                [QColor(0, 0, 0)], (1,))

    def _cont_split_plot(self):
        self.ploti.getAxis("bottom").setTicks(None)
        data = self.valid_data
        _, bins = np.histogram(data, bins=self.binnings[self.number_of_bins])
        gvalues = self.cvar.values
        varcolors = [QColor(*col) for col in self.cvar.colors]
        if self.fitted_distribution:
            gcolors = [c.lighter(130) for c in varcolors]
        else:
            gcolors = varcolors
        nvalues = len(gvalues)
        ys = []
        fitters = []
        prior_sizes = []
        for val_idx in range(nvalues):
            group_data = data[self.valid_group_data == val_idx]
            prior_sizes.append(len(group_data))
            ys.append(np.histogram(group_data, bins)[0])
            if self.fitted_distribution:
                fitters.append(self._fit_approximation(group_data))
        total = len(data)
        prior_sizes = np.array(prior_sizes)
        tot_freqs = np.zeros(len(ys))

        lasti = len(ys[0]) - 1
        for i, x0, x1, freqs in zip(count(), bins, bins[1:], zip(*ys)):
            tot_freqs += freqs
            plotfreqs = tot_freqs.copy() if self.cumulative_distr else freqs
            self._add_bar(
                x0, x1 - x0, 0 if self.stacked_columns else 0.1, plotfreqs,
                gcolors, stacked=self.stacked_columns, expanded=self.show_probs,
                tooltip=self._split_tooltip(
                    self.str_int(x0, x1, not i, i == lasti),
                    np.sum(plotfreqs), total, gvalues, plotfreqs))

        if fitters:
            self._plot_approximations(bins[0], bins[-1], fitters, varcolors,
                                      prior_sizes / len(data))

    def _fit_approximation(self, y):
        def join_pars(pairs):
            strv = self.var.str_val
            return ", ".join(f"{sname}={strv(val)}" for sname, val in pairs)

        def str_params():
            s = join_pars(
                (sname, val) for sname, val in zip(str_names, fitted)
                if sname[0] != "-")
            par = join_pars(
                (sname[1:], val) for sname, val in zip(str_names, fitted)
                if sname[0] == "-")
            if par:
                s += f" ({par})"
            return s

        _, dist, names, str_names = self.Fitters[self.fitted_distribution]
        fitted = dist.fit(y)
        params = {name: val for name, val in zip(names, fitted)}
        return partial(dist.pdf, **params), str_params()

    def _plot_approximations(self, x0, x1, fitters, colors, prior_probs=0):
        x = np.linspace(x0, x1, 100)
        ys = np.empty((len(fitters), 100))
        self.curve_descriptions = [s for _, s in fitters]
        for y, (fitter, _), color in zip(ys, fitters, colors):
            y[:] = fitter(x)
            if self.cumulative_distr:
                y[:] = np.cumsum(y)
        tots = np.sum(ys, axis=0)

        show_probs = self.show_probs and self.cvar is not None
        plot = self.ploti if show_probs else self.plot_pdf

        for y, prior_prob, color in zip(ys, prior_probs, colors):
            if show_probs:
                y_p = y * prior_prob
                y = y_p / (y_p + (tots - y) * (1 - prior_prob))
            plot.addItem(pg.PlotCurveItem(
                x=x, y=y,
                pen=pg.mkPen(width=5, color=color),
                shadowPen=pg.mkPen(width=8, color=color.darker(120))))
        if not show_probs:
            self.plot_pdf.autoRange()

    @staticmethod
    def _split_tooltip(valname, tot_group, total, gvalues, freqs):
        div_group = tot_group or 1
        cs = "white-space:pre; text-align: right;"
        s = f"style='{cs} padding-left: 1em'"
        snp = f"style='{cs}'"
        return f"<table style='border-collapse: collapse'>" \
               f"<tr><th {s}>{escape(valname)}:</th>" \
               f"<td {snp}><b>{int(tot_group)}</b></td>" \
               "<td/>" \
               f"<td {s}><b>{100 * tot_group / total:.2f} %</b></td></tr>" + \
               f"<tr><td/><td/><td {s}>(in group)</td><td {s}>(overall)</td>" \
               "</tr>" + \
               "".join(
                   "<tr>"
                   f"<th {s}>{value}:</th>"
                   f"<td {snp}><b>{int(freq)}</b></td>"
                   f"<td {s}>{100 * freq / div_group:.2f} %</td>"
                   f"<td {s}>{100 * freq / total:.2f} %</td>"
                   "</tr>"
                   for value, freq in zip(gvalues, freqs)) + \
               "</table>"

    def _display_legend(self):
        assert self.is_valid  # called only from replot, so assumes data is OK
        if self.cvar is None:
            if not self.curve_descriptions:
                self._legend.hide()
                return
            self._legend.addItem(
                pg.PlotCurveItem(pen=pg.mkPen(width=5, color=0.0)),
                self.curve_descriptions[0])
        else:
            cvar_values = self.cvar.values
            colors = [QColor(*col) for col in self.cvar.colors]
            descriptions = self.curve_descriptions or repeat(None)
            for color, name, desc in zip(colors, cvar_values, descriptions):
                self._legend.addItem(
                    ScatterPlotItem(pen=color, brush=color, size=10, shape="s"),
                    escape(name + (f" ({desc})" if desc else "")))
        self._legend.show()

    # -----------------------------
    # Bins

    def recompute_binnings(self):
        self.binnings = []
        max_bins = 0
        if self.is_valid and self.var.is_continuous:
            # binning is computed on valid var data, ignoring any cvar nans
            column = self.data.get_column_view(self.var)[0].astype(float)
            if np.any(np.isfinite(column)):
                self.binnings = decimal_binnings(
                    column, min_width=self.min_var_resolution(self.var),
                    add_unique=10, min_unique=5)
                max_bins = len(self.binnings) - 1

        self.controls.number_of_bins.setMaximum(max_bins)
        self.number_of_bins = min(
            max_bins, self._user_var_bins.get(self.var, self.number_of_bins))
        self._set_bin_width_slider_label()

    @staticmethod
    def min_var_resolution(var):
        # pylint: disable=unidiomatic-typecheck
        if type(var) is not ContinuousVariable:
            return 0
        return 10 ** -var.number_of_decimals

    def str_int(self, x0, x1, first, last):
        var = self.var
        sx0, sx1 = var.repr_val(x0), var.repr_val(x1)
        if self.cumulative_distr:
            return f"{var.name} < {sx1}"
        elif first and last:
            return f"{var.name} = {sx0}"
        elif first:
            return f"{var.name} < {sx1}"
        elif last:
            return f"{var.name} ≥ {sx0}"
        elif sx0 == sx1 or x1 - x0 <= self.min_var_resolution(var):
            return f"{var.name} = {sx0}"
        else:
            return f"{sx0} ≤ {var.name} < {sx1}"

    # -----------------------------
    # Selection

    def _on_item_clicked(self, item, modifiers, drag):
        def add_or_remove(idx, add):
            self.drag_operation = [self.DragRemove, self.DragAdd][add]
            if add:
                self.selection.add(idx)
            else:
                if idx in self.selection:
                    # This can be False when removing with dragging and the
                    # mouse crosses unselected items
                    self.selection.remove(idx)

        def add_range(add):
            if self.last_click_idx is None:
                add = True
                idx_range = {idx}
            else:
                from_idx, to_idx = sorted((self.last_click_idx, idx))
                idx_range = set(range(from_idx, to_idx + 1))
            self.drag_operation = [self.DragRemove, self.DragAdd][add]
            if add:
                self.selection |= idx_range
            else:
                self.selection -= idx_range

        self.key_operation = None
        if item is None:
            self.reset_select()
            return

        idx = self.bar_items.index(item)
        if drag:
            # Dragging has to add a range, otherwise fast dragging skips bars
            add_range(self.drag_operation == self.DragAdd)
        else:
            if modifiers & Qt.ShiftModifier:
                add_range(self.drag_operation == self.DragAdd)
            elif modifiers & Qt.ControlModifier:
                add_or_remove(idx, add=idx not in self.selection)
            else:
                if self.selection == {idx}:
                    # Clicking on a single selected bar  deselects it,
                    # but dragging from here will select
                    add_or_remove(idx, add=False)
                    self.drag_operation = self.DragAdd
                else:
                    self.selection.clear()
                    add_or_remove(idx, add=True)
        self.last_click_idx = idx

        self.show_selection()

    def _on_blank_clicked(self):
        self.reset_select()

    def reset_select(self):
        self.selection.clear()
        self.last_click_idx = None
        self.drag_operation = None
        self.key_operation = None
        self.show_selection()

    def _on_end_selecting(self):
        self.apply()

    def show_selection(self):
        self.plot_mark.clear()
        if not self.is_valid:  # though if it's not, selection is empty anyway
            return

        blue = QColor(Qt.blue)
        pen = QPen(QBrush(blue), 3)
        pen.setCosmetic(True)
        brush = QBrush(blue.lighter(190))

        for group in self.grouped_selection():
            group = list(group)
            left_idx, right_idx = group[0], group[-1]
            left_pad, right_pad = self._determine_padding(left_idx, right_idx)
            x0 = self.bar_items[left_idx].x0 - left_pad
            x1 = self.bar_items[right_idx].x1 + right_pad
            item = QGraphicsRectItem(x0, 0, x1 - x0, 1)
            item.setPen(pen)
            item.setBrush(brush)
            if self.var.is_continuous:
                valname = self.str_int(
                    x0, x1, not left_idx, right_idx == len(self.bar_items) - 1)
                inside = sum(np.sum(self.bar_items[i].freqs) for i in group)
                total = len(self.valid_data)
                item.setToolTip(
                    "<p style='white-space:pre;'>"
                    f"<b>{escape(valname)}</b>: "
                    f"{inside} ({100 * inside / total:.2f} %)")
            self.plot_mark.addItem(item)

    def _determine_padding(self, left_idx, right_idx):
        def _padding(i):
            return (self.bar_items[i + 1].x0 - self.bar_items[i].x1) / 2

        if len(self.bar_items) == 1:
            return 6, 6
        if left_idx == 0 and right_idx == len(self.bar_items) - 1:
            return (_padding(0), ) * 2

        if left_idx > 0:
            left_pad = _padding(left_idx - 1)
        if right_idx < len(self.bar_items) - 1:
            right_pad = _padding(right_idx)
        else:
            right_pad = left_pad
        if left_idx == 0:
            left_pad = right_pad
        return left_pad, right_pad

    def grouped_selection(self):
        return [[g[1] for g in group]
                for _, group in groupby(enumerate(sorted(self.selection)),
                                        key=lambda x: x[1] - x[0])]

    def keyPressEvent(self, e):
        def on_nothing_selected():
            if e.key() == Qt.Key_Left:
                self.last_click_idx = len(self.bar_items) - 1
            else:
                self.last_click_idx = 0
            self.selection.add(self.last_click_idx)

        def on_key_left():
            if e.modifiers() & Qt.ShiftModifier:
                if self.key_operation == Qt.Key_Right and first != last:
                    self.selection.remove(last)
                    self.last_click_idx = last - 1
                elif first:
                    self.key_operation = Qt.Key_Left
                    self.selection.add(first - 1)
                    self.last_click_idx = first - 1
            else:
                self.selection.clear()
                self.last_click_idx = max(first - 1, 0)
                self.selection.add(self.last_click_idx)

        def on_key_right():
            if e.modifiers() & Qt.ShiftModifier:
                if self.key_operation == Qt.Key_Left and first != last:
                    self.selection.remove(first)
                    self.last_click_idx = first + 1
                elif not self._is_last_bar(last):
                    self.key_operation = Qt.Key_Right
                    self.selection.add(last + 1)
                    self.last_click_idx = last + 1
            else:
                self.selection.clear()
                self.last_click_idx = min(last + 1, len(self.bar_items) - 1)
                self.selection.add(self.last_click_idx)

        if not self.is_valid or not self.bar_items \
                or e.key() not in (Qt.Key_Left, Qt.Key_Right):
            super().keyPressEvent(e)
            return

        prev_selection = self.selection.copy()
        if not self.selection:
            on_nothing_selected()
        else:
            first, last = min(self.selection), max(self.selection)
            if e.key() == Qt.Key_Left:
                on_key_left()
            else:
                on_key_right()

        if self.selection != prev_selection:
            self.drag_operation = self.DragAdd
            self.show_selection()
            self.apply()

    def keyReleaseEvent(self, ev):
        if ev.key() == Qt.Key_Shift:
            self.key_operation = None
        super().keyReleaseEvent(ev)


    # -----------------------------
    # Output

    def apply(self):
        data = self.data
        selected_data = annotated_data = histogram_data = None
        if self.is_valid:
            if self.var.is_discrete:
                group_indices, values = self._get_output_indices_disc()
            else:
                group_indices, values = self._get_output_indices_cont()
                hist_indices, hist_values = self._get_histogram_indices()
                histogram_data = create_groups_table(
                    data, hist_indices, values=hist_values)
            selected = np.nonzero(group_indices)[0]
            if selected.size:
                selected_data = create_groups_table(
                    data, group_indices,
                    include_unselected=False, values=values)
                annotated_data = create_annotated_table(data, selected)

        self.Outputs.selected_data.send(selected_data)
        self.Outputs.annotated_data.send(annotated_data)
        self.Outputs.histogram_data.send(histogram_data)

    def _get_output_indices_disc(self):
        group_indices = np.zeros(len(self.data), dtype=np.int32)
        col = self.data.get_column_view(self.var)[0].astype(float)
        for group_idx, val_idx in enumerate(self.selection, start=1):
            group_indices[col == val_idx] = group_idx
        values = [self.var.values[i] for i in self.selection]
        return group_indices, values

    def _get_output_indices_cont(self):
        group_indices = np.zeros(len(self.data), dtype=np.int32)
        col = self.data.get_column_view(self.var)[0].astype(float)
        values = []
        for group_idx, group in enumerate(self.grouped_selection(), start=1):
            x0 = x1 = None
            for bar_idx in group:
                minx, maxx, mask = self._get_cont_baritem_indices(col, bar_idx)
                if x0 is None:
                    x0 = minx
                x1 = maxx
                group_indices[mask] = group_idx
            # pylint: disable=undefined-loop-variable
            values.append(
                self.str_int(x0, x1, not bar_idx, self._is_last_bar(bar_idx)))
        return group_indices, values

    def _get_histogram_indices(self):
        group_indices = np.zeros(len(self.data), dtype=np.int32)
        col = self.data.get_column_view(self.var)[0].astype(float)
        values = []
        for bar_idx in range(len(self.bar_items)):
            x0, x1, mask = self._get_cont_baritem_indices(col, bar_idx)
            group_indices[mask] = bar_idx + 1
            values.append(
                self.str_int(x0, x1, not bar_idx, self._is_last_bar(bar_idx)))
        return group_indices, values

    def _get_cont_baritem_indices(self, col, bar_idx):
        bar_item = self.bar_items[bar_idx]
        minx = bar_item.x0
        maxx = bar_item.x1 + (bar_idx == len(self.bar_items) - 1)
        with np.errstate(invalid="ignore"):
            return minx, maxx, (col >= minx) * (col < maxx)

    def _is_last_bar(self, idx):
        return idx == len(self.bar_items) - 1

    # -----------------------------
    # Report

    def get_widget_name_extension(self):
        return self.var

    def send_report(self):
        self.plotview.scene().setSceneRect(self.plotview.sceneRect())
        if not self.is_valid:
            return
        self.report_plot()
        if self.cumulative_distr:
            text = f"Cummulative distribution of '{self.var.name}'"
        else:
            text = f"Distribution of '{self.var.name}'"
        if self.cvar:
            text += f" with columns split by '{self.cvar.name}'"
        self.report_caption(text)


if __name__ == "__main__":  # pragma: no cover
    WidgetPreview(OWDistributions).run(Table("heart_disease.tab"))
