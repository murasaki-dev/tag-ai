import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: flow
    required property var backend
    required property var theme
    required property string flowKey
    property bool showDetails: false
    property bool parallelFlow: false
    property bool showColors: true
    property string detailsButtonText: ""
    property string selectedMethod: ""
    signal details(string method)
    signal colorRequested(string method)
    property var nodes: backend.flows[flowKey] || []
    property string visionModel: backend.configuration.vision_model === "Same as routing" ? backend.configuration.selected_model : backend.configuration.vision_model
    Label { text: flow.parallelFlow ? "Parallel branches · checkboxes enable providers · order does not set priority" : "Drag cards to reorder · checkboxes enable steps"; color: theme.muted; font.pixelSize: 12; Layout.fillWidth: true; wrapMode: Text.WordWrap }
    FlowLink { theme: flow.theme; visible: flow.parallelFlow; branches: flow.nodes.length; Layout.alignment: Qt.AlignHCenter; Layout.fillWidth: false; Layout.preferredWidth: list.width }
    ListView {
        id: list
        Layout.alignment: Qt.AlignHCenter
        Layout.preferredWidth: Math.min(flow.width, contentWidth)
        Layout.maximumWidth: flow.width
        implicitHeight: 300
        orientation: ListView.Horizontal; spacing: 32; clip: true; interactive: false
        model: flow.nodes
        ScrollBar.horizontal: ScrollBar { policy: list.contentWidth > list.width ? ScrollBar.AlwaysOn : ScrollBar.AsNeeded }
        delegate: Item {
            id: node
            required property int index
            required property var modelData
            width: flow.flowKey === "context" ? Math.max(150, Math.min(220, (flow.width - list.spacing * (flow.nodes.length - 1)) / Math.max(1, flow.nodes.length))) : 220
            height: 280
            property real dropOffset: 0
            property int stepNumber: flow.nodes.slice(0, index + 1).filter(n => n.enabled).length
            Item {
                objectName: "flowConnector"
                visible: !flow.parallelFlow && node.index + 1 < flow.nodes.length
                x: node.width; width: list.spacing; height: node.height
                Rectangle { x: 2; y: 100; width: parent.width - 14; height: 2; color: theme.accent; opacity: .7 }
                Label { anchors.right: parent.right; anchors.rightMargin: 2; y: 85; text: flow.parallelFlow ? "+" : "›"; font.pixelSize: 27; color: theme.accent }
                Label { anchors.horizontalCenter: parent.horizontalCenter; y: 117; text: flow.parallelFlow ? "together" : flow.flowKey === "context" ? "no hints" : "no tags"; font.pixelSize: 9; color: theme.muted }
            }
            z: drag.active ? 20 : 0
            FlowCard {
                id: methodCard
                backend: flow.backend; theme: flow.theme
                x: drag.active ? node.dropOffset : 0; width: node.width; height: node.height
                title: backend.flowLabels[modelData.method] || modelData.method
                modelName: modelData.method === "Local Vision LLM" ? flow.visionModel || "Not selected" : ""
                badge: modelData.enabled ? node.stepNumber : "–"
                badgeColor: flow.flowKey === "search" ? backend.historyColors[modelData.method] : theme.accent
                checkable: true; checked: modelData.enabled; configurable: flow.showDetails
                settingsText: flow.detailsButtonText || "Settings"
                timingKey: flow.flowKey === "search" ? ({"IQDB":"iqdb", "SauceNAO":"saucenao", "Local Vision LLM":"localvision"})[modelData.method] : flow.flowKey === "context" ? (modelData.method === "serpapi" ? "web.lens" : "web.cloud") : "database." + modelData.method
                onToggled: value => backend.toggleFlow(flow.flowKey, modelData.method, value)
                onConfigure: flow.details(modelData.method)
                actions: [ToolButton {
                    objectName: flow.showColors ? "methodColor_" + modelData.method : ""
                    visible: flow.flowKey === "search" && flow.showColors
                    implicitWidth: 28; implicitHeight: 28
                    contentItem: Rectangle { implicitWidth: 18; implicitHeight: 18; radius: 3; color: backend.historyColors[modelData.method] || theme.muted; border.color: theme.line }
                    onClicked: flow.colorRequested(modelData.method)
                    ToolTip.visible: hovered; ToolTip.text: "History color · " + modelData.method
                }]
                DragHandler {
                    id: drag
                    enabled: !flow.parallelFlow
                    target: null
                    onActiveTranslationChanged: if (active) node.dropOffset = activeTranslation.x
                    onActiveChanged: {
                        if (!active) {
                            const offset = node.dropOffset
                            node.dropOffset = 0
                            if (Math.abs(offset) > 40) backend.moveFlow(flow.flowKey, modelData.method, index + Math.round(offset / (node.width + list.spacing)))
                        }
                    }
                }
            }
        }
    }
    FlowLink { theme: flow.theme; visible: flow.parallelFlow; branches: flow.nodes.length; merge: true; Layout.alignment: Qt.AlignHCenter; Layout.fillWidth: false; Layout.preferredWidth: list.width }
}
